#!/usr/bin/env python3
"""Issue #6951: [Card Bug] Elspeth Conquers Death - chapter III
return-and-put-counter effect is unsupported.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.81.3):
  Elspeth Conquers Death ({3}{W}{W} Enchantment - Saga):
    "(As this Saga enters and after your draw step, add a lore counter.
     Sacrifice after III.)
     I - Exile target permanent an opponent controls with mana value 3 or
         greater.
     II - Noncreature spells your opponents cast cost {2} more to cast
          until your next turn.
     III - Return target creature or planeswalker card from your graveyard
           to the battlefield. Put a +1/+1 counter or a loyalty counter
           on it."

Reported symptom (v0.42.0, deckbuilder): the deck builder flags the card as
unsupported: `Effect:noncreature, Effect:put`. Chapter II's noncreature-spell
tax and chapter III's "put a +1/+1 counter or a loyalty counter on it"
don't parse.

Parse state on v0.81.3 (observed 2026-09-13 before the run):
  triggers[1] (Chapter 2): execute.effect.type == "Unimplemented",
    name == "noncreature"  -> STILL unsupported (reported Effect:noncreature
    flag persists).
  triggers[2] (Chapter 3): ChangeZone Graveyard->Battlefield (creature-or-
    planeswalker target) with a typed sub_ability chain:
    TargetOnly(ParentTarget) -> ChooseOneOf[ PutCounter P1P1 | PutCounter
    loyalty ] on ParentTarget. No Unimplemented nodes -> the reported
    Effect:put flag no longer applies at parse level.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Elspeth Conquers Death, 12x Grizzly Bears, 24x Plains, 20x Forest.
  P1: 12x Lightning Bolt, 48x Mountain.
  T1 P0 Forest. T2 P1 Mountain. T3 P0 Forest + Bear. T4 P1 Mountain + Bolt
  the Bear (Bear -> P0 graveyard). T5/T7 P0 Plains. T6/T8 P1 Mountain.
  T9 P0 Forest (5 lands: 3F 2P) + cast ECD {3}{W}{W}; chapter I fizzles
  (P1 controls no MV>=3 permanent). T10 P1 Mountain. T11 P0 draw -> lore 2
  -> chapter II (Unimplemented, no-op). T12 P1 Bolts P0 face inside the
  chapter-II tax window; the tax is unimplemented so the Bolt must cost
  {R} (1 Mountain tapped). T13 P0 draw -> lore 3 -> chapter III: target the
  Bear in P0's graveyard, it returns, then the counter-choice is offered;
  choose +1/+1 -> Bear 3/3 with a +1/+1 counter.

Assertions:
  A1_ch2_unimplemented  card-data: chapter II effect is Unimplemented
                        ("noncreature"). passed => the reported chapter-II
                        gap persists (deck-builder flag still applies).
  A2_ch3_typed          card-data: chapter III subtree has no Unimplemented
                        nodes and carries the ChooseOneOf counter choice.
                        passed => chapter-III parse gap closed.
  A3_reached_ch3        PRE: ECD on BF at lore 3 with the Bear in P0's
                        graveyard and the chapter-3 trigger pending.
  A4_tax_absent         P1's in-window Bolt tapped exactly 1 Mountain
                        (no {2} tax applied).
  A5_returned           POST: the Bear is on P0's battlefield.
  A6_counter            POST: that Bear is 3/3 with a +1/+1 counter.
  A7_cleanup            POST: stack empty, waiting_for in (Priority, None).

Verdict rule: blocked iff A3 fails (never reached chapter III). reproduced
iff A1 passes (chapter-II unsupported aspect persists on v0.81.3; A2/A5/A6
describe the chapter-III status). not-reproduced iff A1 fails and A2-A7
all pass.

Evidence: evidence/6951/<run-id>/pre.json, mid.json, post.json, run.json,
manifest.sha256, summary.png, scenario_6951.py, wire_log.jsonl,
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
RUN_ID = os.environ.get("RUN_ID", "20260913-6951")
ISSUE = 6951
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ECD = "elspeth conquers death"
BEAR = "grizzly bears"
BOLT = "lightning bolt"
PLAINS = "plains"
FOREST = "forest"
MOUNTAIN = "mountain"
LANDS = (PLAINS, FOREST, MOUNTAIN)

P0_DECK = [(ECD, 4), (BEAR, 12), (PLAINS, 24), (FOREST, 20)]
P1_DECK = [(BOLT, 12), (MOUNTAIN, 48)]

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


def yard_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("owner", o.get("controller")) == pid
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in LANDS):
            out.append(int(oid))
    return out


def tapped_mountains(state, pid):
    n = 0
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and o.get("tapped") and nm == MOUNTAIN):
            n += 1
    return n


def counters_of(obj):
    c = obj.get("counters")
    if isinstance(c, dict):
        return c
    if isinstance(c, list):
        out = {}
        for e in c:
            if isinstance(e, dict):
                k = e.get("type") or e.get("kind") or e.get("name")
                out[str(k)] = e.get("count", 1)
            else:
                out[str(e)] = out.get(str(e), 0) + 1
        return out
    return {}


def lore_of(state, oid):
    o = get_obj(state, oid)
    ctrs = counters_of(o)
    for k, v in ctrs.items():
        if "lore" in str(k).lower():
            try:
                return int(v)
            except Exception:
                return v
    return 0


def has_p1p1(obj):
    ctrs = counters_of(obj)
    for k, v in ctrs.items():
        kl = str(k).lower()
        if "p1p1" in kl or "+1/+1" in kl or "plusone" in kl.replace(" ", ""):
            try:
                if int(v) >= 1:
                    return True
            except Exception:
                return True
    return False


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


def deep_refs(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("reference", "object_id", "objectId", "target_id") and isinstance(v, (int, str)):
                out.append(v)
            else:
                deep_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            deep_refs(v, out)
    return out


def ref_of(choice):
    refs = []
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        deep_refs(d, refs)
    for r in refs:
        try:
            return int(r)
        except Exception:
            continue
    return refs[0] if refs else None


def idx_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "optionIndex" \
                and "value" in d:
            return str(d["value"])
    return None


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


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


def ch3_trigger_on_stack(state, ecd_oid):
    for e in state.get("stack") or []:
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        desc = ""
        ab = e.get("ability") or kind.get("ability") or {}
        if isinstance(ab, dict):
            desc = str(ab.get("description") or "")
        if e.get("source_id") == ecd_oid and "chapter 3" in desc.lower():
            return True
        if ktype == "TriggeredAbility" and "chapter 3" in desc.lower():
            return True
    return False


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_ch2_unimplemented", "A2_ch3_typed", "A3_reached_ch3",
            "A4_tax_absent", "A5_returned", "A6_counter", "A7_cleanup")}

    # ---- A1/A2 (parse) up front, from the pinned card-data.json ----
    cd_path = f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json"
    try:
        cd = json.load(open(cd_path))
        e = cd["elspeth conquers death"]
        trigs = e.get("triggers", [])
        ch2 = next((t for t in trigs if t.get("saga_chapter") == 2), {})
        ch3 = next((t for t in trigs if t.get("saga_chapter") == 3), {})
        ch2eff = ((ch2.get("execute") or {}).get("effect") or {})
        ok1 = (ch2eff.get("type") == "Unimplemented"
               and ch2eff.get("name") == "noncreature")
        ass["A1_ch2_unimplemented"] = "passed" if ok1 else "failed"
        notes.append(f"A1: chapter II effect = {ch2eff.get('type')}/"
                     f"{ch2eff.get('name')} (expected Unimplemented/noncreature)")
        wire("parse_ch2", {"effect": ch2eff})
        blob = json.dumps(ch3)
        has_unimpl = '"Unimplemented"' in blob
        has_choice = ("ChooseOneOf" in blob and "PutCounter" in blob
                      and "P1P1" in blob and "loyalty" in blob)
        ok2 = (not has_unimpl) and has_choice
        ass["A2_ch3_typed"] = "passed" if ok2 else "failed"
        notes.append(f"A2: chapter III has_unimplemented={has_unimpl} "
                     f"has_chooseoneof_putcounter={has_choice}")
        wire("parse_ch3", {"trigger": ch3})
    except Exception as ex:
        ass["A1_ch2_unimplemented"] = "failed"
        ass["A2_ch3_typed"] = "failed"
        notes.append(f"parse check failed: {ex}")

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"RUN_ID={RUN_ID}")
    kept = {}

    ST = {"bear_cast": False, "bolt1_done": False, "bolt1_cast": False,
          "ecd_cast": False, "ecd_oid": None,
          "ch2_seen": False, "ch2_at": None, "mid_exported": False,
          "bolt2_done": False, "bolt2_cast": False, "bolt2_tapped": None,
          "ch3_seen": False, "pre_exported": False,
          "ch3_targeted": False, "ch3_target_oid": None,
          "counter_answered": False, "counter_choice": None,
          "post_exported": False, "post_at": None,
          "ecd_cast_at": None}
    obs = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
           "tick_errors": [], "stack_trace": []}
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
        for fn in ("pre", "mid", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        pre, mid, post = (states.get(k) for k in ("pre", "mid", "post"))

        # ---- A3: reached chapter III ----
        if pre is not None:
            ecd = bf_id(pre, 0, ECD)
            lore = lore_of(pre, ecd) if ecd else None
            bear_yard = bool(yard_ids(pre, 0, BEAR))
            ok = (ecd is not None and (lore == 3 or ST["ch3_targeted"])
                  and bear_yard)
            ass["A3_reached_ch3"] = "passed" if ok else "failed"
            notes.append(f"A3: pre ecd={ecd} lore={lore} bear_in_yard={bear_yard} "
                         f"ch3_targeted={ST['ch3_targeted']}")
        else:
            ass["A3_reached_ch3"] = "failed"
            notes.append("A3 failed: pre.json missing")

        # ---- A4: chapter-II tax absent ----
        if ST["bolt2_tapped"] is not None:
            ok = (ST["bolt2_tapped"] == 1)
            ass["A4_tax_absent"] = "passed" if ok else "failed"
            notes.append(f"A4: P1 in-window Bolt tapped {ST['bolt2_tapped']} "
                         f"Mountain(s) (expected 1; 3 would mean the chapter-II "
                         f"tax applied)")
        else:
            ass["A4_tax_absent"] = "failed"
            notes.append("A4 failed: bolt2 tapped-mana observation missing")

        # ---- A5/A6: Bear returned with +1/+1 counter ----
        if post is not None:
            bears = [(oid, get_obj(post, oid))
                     for oid in bf_ids(post, 0, BEAR)]
            ok5 = len(bears) >= 1
            ass["A5_returned"] = "passed" if ok5 else "failed"
            notes.append(f"A5: P0 Bears on BF in post: {len(bears)}")
            ok6 = False
            for oid, o in bears:
                pw, tw = o.get("power"), o.get("toughness")
                p1p1 = has_p1p1(o)
                notes.append(f"A6: bear oid={oid} P/T={pw}/{tw} "
                             f"+1/+1counter={p1p1} counters={counters_of(o)}")
                if pw == 3 and tw == 3 and p1p1:
                    ok6 = True
            ass["A6_counter"] = "passed" if ok6 else "failed"
        else:
            ass["A5_returned"] = "failed"
            ass["A6_counter"] = "failed"
            notes.append("A5/A6 failed: post.json missing")

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
        if ass["A3_reached_ch3"] != "passed":
            verdict = "blocked"
        elif ass["A1_ch2_unimplemented"] == "passed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: chapter II noncreature tax is "
                         "still Unimplemented on v0.81.3 (deck-builder "
                         "Effect:noncreature flag persists); chapter III "
                         f"parse-fixed={ass['A2_ch3_typed']} "
                         f"returned={ass['A5_returned']} "
                         f"counter={ass['A6_counter']}")
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-13",
            "server": SERVER_IDENTITY,
            "server_run_note": "isolated v0.81.3 server on 127.0.0.1:9374, "
                               "started for this run (log in "
                               f"runs/{RUN_ID}/server.log)",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6951.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": ST,
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               "scenario_6951.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI / deckbuilder UI not exercised; the deck-builder "
                "'unsupported' flag is evidenced via the card-data parse "
                "check (Unimplemented nodes drive the flag).",
                "Dense playsets are a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_6951.py",
                    f"{EVDIR}/scenario_6951.py")
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied scenario_6951.py and server.log into EVDIR")
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
        W, H = 1000, 960
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #6951 - Elspeth Conquers Death",
               fill=(235, 240, 250))
        y += 28
        d.text((24, y),
               "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - saga "
               "chapters II+III",
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
            "A1_ch2_unimplemented": "card-data: chapter II still Unimplemented(noncreature)",
            "A2_ch3_typed": "card-data: chapter III fully typed (ChooseOneOf counters)",
            "A3_reached_ch3": "PRE: ECD lore 3, Bear in P0 yard, ch3 pending",
            "A4_tax_absent": "P1 in-window Bolt cost {R} (1 Mountain tapped)",
            "A5_returned": "POST: Bear back on P0 battlefield",
            "A6_counter": "POST: Bear 3/3 with +1/+1 counter",
            "A7_cleanup": "stack empty, game proceeds",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "not-run")
            col = (120, 220, 120) if v == "passed" else (
                (255, 90, 90) if v == "failed" else (160, 160, 160))
            d.text((36, y), f"{k}: {v} - {lab}", fill=col)
            y += 24
        y += 10
        d.text((24, y), "Bear across states:", fill=(200, 210, 225))
        y += 24
        for label in ("pre", "mid", "post"):
            st = states.get(label)
            if st is not None:
                bears = [(oid, get_obj(st, oid))
                         for oid in bf_ids(st, 0, BEAR)]
                ecd = bf_id(st, 0, ECD)
                line = (f"{label:>4}: ecd={ecd} lore={lore_of(st, ecd) if ecd else '-'} "
                        f"bears_bf={len(bears)} "
                        + (" ".join(f"{o.get('power')}/{o.get('toughness')}"
                                    for _, o in bears) if bears else ""))
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
        files = ["pre.json", "mid.json", "post.json", "run.json",
                 "scenario_6951.py", "wire_log.jsonl", "scenario_run.log",
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
        need = 3 if pid == 0 else 2
        ok = lands >= need or mulls >= 2
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
                if t == ECD:
                    return 3
                if t == BEAR:
                    return 2
                if t == BOLT:
                    return 2
                return 0 if t in LANDS else 1

            pick = sorted(chs, key=rank)[0]
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def target_selection_tick(c, pid, tag, st, state, pick_fn,
                                   done_key, log_name):
        """Answer a TargetSelection prompt using pick_fn(choice)->bool."""
        if (wf_of(state).get("type") or "") != "TargetSelection":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            wire("target_selection",
                 {"who": tag, "name": log_name, "n": len(chs),
                  "texts": [choice_text(ch)[:60] for ch in chs][:8],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            pick = None
            for ch in chs:
                try:
                    if pick_fn(ch, state):
                        pick = ch
                        break
                except Exception as e:
                    say(f"[{tag}] pick_fn error: {e}")
            if pick is None:
                say(f"[{tag}] {log_name}: no matching candidate "
                    f"({len(chs)} offered); NOT answering")
                obs["unexpected_prompts"].append(
                    {"who": tag, "kind": log_name, "n_choices": len(chs)})
                entry["done"] = True
                return True
            oid = ref_of(pick)
            ST["last_target_oid"] = oid
            ST[done_key] = True
            say(f"[{tag}] {log_name}: chose oid={oid} "
                f"({choice_text(pick)[:60]})")
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
            return True
        return False

    async def choose_one_branch_tick(c, tag, st, state):
        """Handle the chapter-III ChooseOneOf counter prompt.

        waiting_for.type == "ChooseOneOfBranch"; the two choices carry no
        branch text, only optionIndex value surfaces ("0"/"1"). The parse
        order is branches[0] = PutCounter P1P1 ("put a +1/+1 counter"),
        branches[1] = PutCounter loyalty, so optionIndex 0 is the +1/+1
        branch; the resulting state (A6) verifies the counter landed.
        """
        if (wf_of(state).get("type") or "") != "ChooseOneOfBranch":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False})
            if entry.get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            wire("counter_choice",
                 {"who": tag, "n": len(chs),
                  "texts": [choice_text(ch)[:80] for ch in chs],
                  "indexes": [idx_of(ch) for ch in chs],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            pick = next((ch for ch in chs
                         if "+1/+1" in choice_text(ch).lower()), None)
            if pick is None:
                pick = next((ch for ch in chs if idx_of(ch) == "0"), chs[0])
            ST["counter_choice"] = (f"{choice_text(pick)[:40]} "
                                    f"(optionIndex {idx_of(pick)})")
            ST["counter_answered"] = True
            say(f"[{tag}] counter choice: picked '{ST['counter_choice']}'")
            await answer_vi(c, opp, pick, tag)
            entry["done"] = True
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
            if time.time() - entry["t0"] < 30:
                continue
            pick = chs[0] if chs else None
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 30s stall")
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

    def pick_bear_bf(ch, state):
        oid = ref_of(ch)
        if oid is None:
            return False
        o = get_obj(state, oid)
        return (lname(state, oid) == BEAR and o.get("zone") == "Battlefield"
                and o.get("controller") == 0)

    def pick_player0(ch, state):
        return seat_of(ch) == 0

    def pick_bear_yard(ch, state):
        oid = ref_of(ch)
        if oid is None:
            return False
        o = get_obj(state, oid)
        return (lname(state, oid) == BEAR and o.get("zone") == "Graveyard")

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
        # track ECD on BF + lore
        ecd = bf_id(state, 0, ECD)
        if ecd is not None:
            if ST["ecd_oid"] is None:
                ST["ecd_oid"] = int(ecd)
                say(f"ECD on BF: oid={ecd}")
            lore = lore_of(state, ecd)
            if lore >= 2 and not ST["ch2_seen"]:
                ST["ch2_seen"] = True
                ST["ch2_at"] = time.time()
                say(f"chapter II reached (lore={lore})")
                wire("chapter2", {"lore": lore})
            if lore >= 3 and not ST["ch3_seen"]:
                ST["ch3_seen"] = True
                say(f"chapter III reached (lore={lore})")
                wire("chapter3", {"lore": lore})
        if await discard_tick(p0, 0, "P0", st, state):
            return
        # chapter-III target selection: Bear in P0 graveyard. (With a single
        # legal candidate the engine auto-targets, so this covers only the
        # prompted case.)
        if (ST["ch3_seen"] and not ST["ch3_targeted"]
                and not ST["counter_answered"]
                and await target_selection_tick(
                    p0, 0, "P0", st, state, pick_bear_yard,
                    "ch3_targeted", "ch3-target")):
            return
        # counter choice after the target is locked (or auto-targeted)
        if (ST["ch3_seen"] and not ST["counter_answered"]
                and await choose_one_branch_tick(p0, "P0", st, state)):
            return
        # MID: chapter II seen, stack empty, 6s grace
        if (ST["ch2_seen"] and not ST["mid_exported"]
                and not (state.get("stack") or [])
                and time.time() - (ST["ch2_at"] or 0) > 6):
            if await export_named("mid"):
                ST["mid_exported"] = True
                say("MID exported: after chapter II")
        # PRE: chapter III reached, Bear still in yard, trigger pending
        if (ST["ch3_seen"] and not ST["pre_exported"]
                and yard_ids(state, 0, BEAR)):
            if await export_named("pre"):
                ST["pre_exported"] = True
                say("PRE exported: chapter III pending, Bear in yard")
        # POST: counter answered, Bear on BF, stack empty, 8s grace
        bears_bf = bf_ids(state, 0, BEAR)
        if (ST["counter_answered"] and bears_bf
                and not ST["post_exported"] and not (state.get("stack") or [])):
            if ST["post_at"] is None:
                ST["post_at"] = time.time()
        if (ST["post_at"] is not None and not ST["post_exported"]
                and time.time() - ST["post_at"] > 8
                and not (state.get("stack") or [])):
            if await export_named("post"):
                ST["post_exported"] = True
                say("POST exported: Bear returned with counter")
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
            if (not ST["bear_cast"] and BEAR in hn and lands >= 2
                    and not bf_ids(state, 0, BEAR)
                    and not yard_ids(state, 0, BEAR)):
                coid = await cast_named(p0, acts, state, BEAR, "P0")
                if coid is not None:
                    ST["bear_cast"] = True
                    return
            if (not ST["ecd_cast"] and ECD in hn and lands >= 5
                    and yard_ids(state, 0, BEAR)):
                coid = await cast_named(p0, acts, state, ECD, "P0")
                if coid is not None:
                    ST["ecd_cast"] = True
                    ST["ecd_cast_at"] = time.time()
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
        # Bolt target selections (both Bolts)
        if wtype == "TargetSelection":
            if not ST["bolt1_done"]:
                if await target_selection_tick(
                        p1, 1, "P1", st, state, pick_bear_bf,
                        "bolt1_done", "bolt1-target-bear"):
                    return
            elif ST["bolt2_cast"] and not ST["bolt2_done"]:
                if await target_selection_tick(
                        p1, 1, "P1", st, state, pick_player0,
                        "bolt2_done", "bolt2-target-p0"):
                    return
        if await discard_tick(p1, 1, "P1", st, state):
            return
        # measure mana paid for bolt2 right after the cast resolves
        if (ST["bolt2_cast"] and ST["bolt2_tapped"] is None
                and ST["bolt2_done"] and not (state.get("stack") or [])):
            ST["bolt2_tapped"] = tapped_mountains(state, 1)
            say(f"bolt2 resolved: P1 tapped Mountains = {ST['bolt2_tapped']}")
            wire("bolt2_mana", {"tapped_mountains": ST["bolt2_tapped"]})
        if not my_priority(state, 1):
            if await generic_prompt(p1, 1, "P1", st, state):
                return
            return
        # ---- P1 priority ----
        in_flight = bool(state.get("stack") or [])
        if (not in_flight
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1):
            hn = hand_lnames(state, 1)
            lands = len(untapped_lands(state, 1))
            # bolt1: kill the Bear as soon as possible
            if (not ST["bolt1_cast"] and BOLT in hn and lands >= 1
                    and bf_ids(state, 0, BEAR)):
                coid = await cast_named(p1, acts, state, BOLT, "P1")
                if coid is not None:
                    ST["bolt1_cast"] = True
                    return
            # bolt2: in-window face Bolt after chapter II
            if (ST["ch2_seen"] and not ST["bolt2_cast"] and BOLT in hn
                    and lands >= 1):
                coid = await cast_named(p1, acts, state, BOLT, "P1")
                if coid is not None:
                    ST["bolt2_cast"] = True
                    say("bolt2 cast (in chapter-II tax window)")
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
        if ST["post_exported"] and time.time() - (ST.get("post_at") or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        # watchdog: ECD cast but chapters never advanced
        if (ST["ecd_cast"] and not ST["ch2_seen"]
                and time.time() - (ST["ecd_cast_at"] or 0) > 420):
            notes.append("watchdog: 420s after ECD cast with no chapter II; "
                         "saga lore counters may not advance")
            await finish()
            return
        if time.time() - t0 > 1200 and ST["post_exported"]:
            notes.append("watchdog: 1200s elapsed; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            ecd = bf_id(s, 0, ECD)
            stk = []
            for e in s.get("stack") or []:
                ab = e.get("ability") or {}
                stk.append(str(ab.get("description") or e.get("kind"))[:40])
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0life={life_of(s, 0)} "
                f"ecd={ecd} lore={lore_of(s, ecd) if ecd else '-'} "
                f"bear_bf={bf_ids(s, 0, BEAR)} bear_yard={yard_ids(s, 0, BEAR)} "
                f"stack={stk} ST_bolt2={ST['bolt2_cast']}/{ST['bolt2_done']} "
                f"ch3={ST['ch3_seen']}/{ST['ch3_targeted']}/{ST['counter_answered']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
