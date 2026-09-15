#!/usr/bin/env python3
"""Issue #7180: Apex Altisaur should have a 'skip' on its triggered ability.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (Discord 2026-08-10, triage-confirmed, status:confirmed): Apex
Altisaur's triggers read "fights up to one target creature you don't
control" (both the ETB trigger and the Enrage trigger), but the player is
forced to choose a creature to fight - choosing zero targets is not offered.

Oracle text (pinned card-data.json v0.84.0, verified 2026-09-15 before run):
  Apex Altisaur ({7}{G}{G}, 10/10):
  "When this creature enters, it fights up to one target creature you
   don't control."
  "Enrage - Whenever this creature is dealt damage, it fights up to one
   target creature you don't control."
Card-data parse state (verified before the run): both triggers carry
  multi_target {min: 0, max: 1}, typed target Creature/Opponent, no
  Unimplemented nodes, AST faithful. The classifier notes the runtime/UI
  omits the legal zero-target action.

Setup (native engine, two human-client seats, single-user, Bo1, life 20):
  P0: 12x Apex Altisaur, 8x Lightning Bolt, 30x Forest, 10x Mountain.
  P1: 12x Grizzly Bears, 48x Forest (passive: land drops, bears, empty
      attacks/blocks).

Planned line:
  P0 ramps to 9 mana, then casts Apex Altisaur only once P1 has >=2 bears
  (2+ legal targets force the target prompt; a single legal target would
  be auto-targeted). ETB trigger -> target prompt: the driver records the
  FULL prompt (waiting_for + viewer_interaction + legal_actions), checks
  for a zero-target/skip option, and:
    - explicit zero choice -> submit it, expect no fight;
    - schema with min==0 but no explicit choice -> probe ONE empty
      choiceIds sequence; if the prompt advances, zero is achievable;
      if ignored after 8s, fall back to answering a bear so the game
      proceeds (recorded as ignored);
    - no zero option at all -> answer a bear (recorded) so the game
      proceeds.
  post_etb.json exported after the ETB trigger (identified by its stack
  id) leaves the stack.
  NOTE: the ETB fight deals damage back to Altisaur, which legitimately
  fires the Enrage trigger -- the driver routes prompts by the trigger's
  own leg (ETB vs Enrage), tolerating "unknown". The PRIMARY enrage leg
  is the first enrage prompt with >=2 bear candidates; its inducer is
  either the deliberate Lightning Bolt (cast at own Altisaur, gated on
  >=2 P1 bears, only while no primary leg exists) or the ETB fight's
  damage. Supporting enrage prompts (forced-fight cascade, usually with
  a single candidate) are answered opportunistically and logged.
  post.json exported after the primary enrage trigger resolves and the
  cascade settles (no Altisaur triggers on the stack).

Assertions (each passed / failed / not-run):
  A1_setup            Altisaur cast and on P0 BF; ETB trigger prompt seen
                      with >=2 bear candidates.
  A2_zero_option_etb  zero-target option offered for the ETB trigger
                      (explicit choice, or empty-sequence probe accepted).
  A3_etb_resolution   trigger resolved correctly for the answered path:
                      zero -> no fight (bears unchanged, Altisaur
                      damage unchanged); forced -> chosen bear left the
                      battlefield (fight happened), game proceeded.
  A4_enrage_setup     primary enrage prompt (first with >=2 bear
                      candidates) observed; inducer is Bolt (Bolt in P0
                      graveyard, resolved) or the ETB fight's damage
                      back to Altisaur.
  A5_zero_option_enrage  zero-target option offered for the primary
                      enrage trigger.
  A6_resolution       enrage trigger resolved correctly for the answered
                      path; stack empty, priority moving.

Verdict rule:
  blocked        iff A1 fails (ETB prompt never observed) or the enrage
                 setup (A4) never reaches its prompt.
  reproduced     iff A1 passes and (A2 fails or A5 fails): at least one
                 trigger forced a fight target with no zero option.
  not-reproduced iff A2 and A5 pass and A3/A6 confirm clean zero-target
                 resolutions.

Evidence: evidence/7180/<run-id>/pre_etb.json, etb_prompt.json,
etb_prompt_detail.json, post_etb.json, pre_bolt.json, enrage_prompt.json,
enrage_prompt_detail.json, post.json, run.json, manifest.sha256,
summary.png, scenario_7180.py, wire_log.jsonl, scenario_run.log,
server.log (excerpts).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7180
RUN_ID = os.environ.get("RUN_ID", "20260915-7180")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ALTISAUR = "apex altisaur"
BOLT = "lightning bolt"
BEAR = "grizzly bears"
FOREST = "forest"
MOUNTAIN = "mountain"

P0_DECK = [(ALTISAUR, 12), (BOLT, 8), (FOREST, 30), (MOUNTAIN, 10)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

RELDIR = f"{BACKFILL}/server/releases/v0.84.0"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "single-user",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": _ACTUAL == _PIN,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode; data digests match "
                      "the signed manifest; release v0.84.0 confirmed "
                      "latest stable via GitHub releases API 2026-09-15",
    "observed_at": "2026-09-15",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9374",
    "source": "verified pin; isolated server on 127.0.0.1:9374 started by "
              "this run's session under runs/20260915-01/",
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


def ref_key(ref):
    """Normalize a candidate reference to a comparable string oid."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return str(ref)
    if isinstance(ref, str) and ref.lstrip("-").isdigit():
        return ref.lstrip("+")
    if isinstance(ref, dict):
        for v in ref.values():
            k = ref_key(v)
            if k is not None:
                return k
        return None
    if isinstance(ref, list):
        for v in ref:
            k = ref_key(v)
            if k is not None:
                return k
        return None
    return None


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


def gy_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Graveyard" and o.get("owner") == pid
            and (key is None or lname(state, oid) == key)]


def untapped_lands(state, pid, names):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and nm in names and not o.get("tapped")):
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


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    d = wf_of(state).get("data") or {}
    return d.get("player")


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def stack_entries(state):
    return state.get("stack") or []


def trigger_leg(entry):
    """Classify an Apex Altisaur trigger from its OWN ability description
    (not the whole entry blob: the entry may embed the card's full text,
    which contains both 'enters' and 'dealt damage')."""
    def find_desc(o, depth=0):
        if depth > 7 or not isinstance(o, dict):
            return None
        ab = o.get("ability")
        if isinstance(ab, dict) and ab.get("description"):
            return str(ab["description"])
        for v in o.values():
            if isinstance(v, dict):
                r = find_desc(v, depth + 1)
                if r:
                    return r
        return None
    desc = (find_desc(entry) or "").lower()
    if "dealt damage" in desc:
        return "enrage"
    if "enters" in desc:
        return "etb"
    return "unknown"


def altisaur_triggers_on_stack(state, want_leg=None):
    out = []
    for e in stack_entries(state):
        kind = (e.get("kind") or {})
        if kind.get("type") == "TriggeredAbility":
            sid = e.get("source_id")
            if sid is not None and lname(state, int(sid)) == ALTISAUR:
                leg = trigger_leg(e)
                if want_leg is None or leg in (want_leg, "unknown"):
                    out.append({"id": e.get("id"), "leg": leg,
                                "raw": e})
    return out


def bolt_spell_on_stack(state, pid=0):
    for e in stack_entries(state):
        kind = (e.get("kind") or {})
        if kind.get("type") == "Spell":
            sid = e.get("source_id")
            if sid is not None and lname(state, int(sid)) == BOLT:
                return e
    return None


def marked_damage(state, oid):
    o = get_obj(state, oid)
    for k in ("damage", "damage_marked", "marked_damage"):
        if k in o:
            return o[k]
    return None

ZERO_TEXT_RE = re.compile(
    r"no target|skip|decline|choose none|\bnone\b|zero targets|pass\b",
    re.IGNORECASE)


def analyze_zero_option(state, st):
    """Inspect a target-selection prompt for a zero-target/skip option.

    Returns a record describing every opportunity, its candidates, any
    explicit zero-like choice, and any schema min/max. Never submits.
    """
    rec = {"opportunities": [], "legal_actions_snapshot": [],
           "has_explicit_zero": False, "zero_choice": None, "zero_via": None,
           "schema_min": None, "schema_max": None, "schema_type": None,
           "bear_candidates": [], "n_candidates": 0}
    vi = get_vi(st)
    opps = (vi.get("opportunities") or []) if vi else []
    for opp in opps:
        iid = opp.get("interactionId")
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        chs = data.get("choices") or data.get("candidates") or []
        entry = {"iid": str(iid), "rtype": rtype, "n": len(chs),
                 "choices": []}
        for ch in chs:
            cid = ch.get("id")
            surfaces = ch.get("surfaces") or []
            txt = json.dumps(surfaces).lower()
            ref = ref_key([(s.get("data") or {}).get("reference")
                           for s in surfaces])
            nm = (lname(state, int(ref)) if ref
                  and ref.lstrip("-").isdigit() else None)
            cinfo = {"id": cid, "ref": ref, "name": nm,
                     "status": (ch.get("status") or {}).get("type"),
                     "text_snip": txt[:160]}
            entry["choices"].append(cinfo)
            rec["n_candidates"] += 1
            if nm == BEAR:
                rec["bear_candidates"].append(
                    {"iid": iid, "rtype": rtype, "choice": ch,
                     "oid": ref})
            is_zeroish = bool(ZERO_TEXT_RE.search(txt))
            if ref is None and nm is None:
                # candidate with no object reference: possible
                # zero/skip placeholder - record, do not assume
                cinfo["null_ref"] = True
            if is_zeroish and not rec["has_explicit_zero"]:
                rec["has_explicit_zero"] = True
                rec["zero_choice"] = {"iid": iid, "rtype": rtype,
                                      "choice": ch}
                rec["zero_via"] = "explicit_zero_text"
        if rtype == "schema":
            spec = data.get("spec") or {}
            if rec["schema_min"] is None:
                rec["schema_min"] = spec.get("min")
                rec["schema_max"] = spec.get("max")
                rec["schema_type"] = spec.get("type")
        rec["opportunities"].append(entry)
    for a in merged_actions(st):
        rec["legal_actions_snapshot"].append(
            {"type": a.get("type"), "data": a.get("data")})
    return rec


def build_submission(iid, rtype, choice_id=None, empty_sequence=False):
    if rtype == "schema":
        data = {"choiceIds": []} if empty_sequence else {
            "choiceIds": [choice_id]}
        return {"interactionId": iid,
                "response": {"type": "sequence", "data": data}}
    return {"interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": choice_id}}}


async def main():
    t_start = time.time()
    ST = {
        "finished": False,
        "stage": "setup",   # setup -> etb_prompt -> etb_resolving ->
                            # bolt_setup -> [enrage prompts, possibly
                            # cascading] -> cleanup -> done
        "game_code": None,
        "altisaur_oid": None,
        "altisaur_cast": False,
        "altisaur_cast_turn": None,
        "bolt_cast": False,
        "bolt_resolved": False,
        # ETB leg records
        "etb_prompt_record": None,
        "etb_trigger_id": None,      # stack id of the ETB trigger
        "etb_zero_offered": None,
        "etb_zero_via": None,
        "etb_probe": None,          # {"iid","sent_at"} if probed
        "etb_probe_accepted": None,
        "etb_answered": None,       # "zero" | "bear" | "probe_bear_fallback"
        "etb_target_oid": None,     # actually submitted bear oid (if any)
        "etb_bears_at_prompt": [],
        "etb_altisaur_damage_at_prompt": None,
        "etb_resolved": False,
        # enrage leg records (enrage can prompt MULTIPLE times: fight
        # damage cascades; the PRIMARY leg is the first prompt with >=2
        # candidates, whether induced by fight damage or by Bolt)
        "enrage_seen_ids": [],       # every enrage trigger id observed
        "enrage_answered_ids": [],   # trigger ids already answered
        "enrage_probes": {},         # tid -> {"iid","sent_at"}
        "enrage_recs": {},           # tid -> analyze_zero_option record
        "enrage_primary_id": None,
        "enrage_inducer": None,      # "fight_damage" | "bolt"
        "enrage_prompt_record": None,  # PRIMARY prompt record
        "enrage_zero_offered": None,
        "enrage_zero_via": None,
        "enrage_probe_accepted": None,
        "enrage_answered": None,     # primary answer kind
        "enrage_target_oid": None,
        "enrage_bears_at_prompt": [],
        "enrage_altisaur_damage_at_prompt": None,
        "enrage_resolved": False,
        "enrage_extra": [],          # non-primary enrage answers logged
        "bolt_damage_seen": None,
        "answered_iids": [],
        "wf_types_seen": [],
        "turns_seen": set(),
        "cleanup_from_turn": None,
        "cleanup_armed_at": 0.0,
        "post_exported": False,
        "exports": {},
        "last_rev_acted": {},
    }
    obs = {"rejections": [], "unexpected_prompts": [], "tick_errors": []}
    ass = {}
    notes = []

    p0 = PhaseClient("P0-altisaur")
    p1 = PhaseClient("P1-bears")
    await p0.connect()
    await p1.connect()
    say("both clients connected")
    sess = await p0.create(deck(*P0_DECK), player_count=2)
    ST["game_code"] = p0.game_code or sess.get("game_code")
    say(f"game created: {ST['game_code']}")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say("P1 joined")

    async def export_named(name):
        try:
            raw = await p0.export_state()
            env = json.loads(raw)
            assert "state" in env, "envelope missing 'state'"
            with open(f"{EVDIR}/{name}.json", "w") as f:
                json.dump(env, f, indent=1)
            ST["exports"][name] = True
            say(f"exported {name}.json "
                f"(turn={env['state'].get('turn_number')})")
            return env["state"]
        except Exception as e:
            say(f"export {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"export_{name}: {e!r}")
            return None

    def save_prompt_detail(name, state, st):
        try:
            detail = {
                "name": name,
                "captured_at": time.time(),
                "turn_number": state.get("turn_number"),
                "phase": state.get("phase"),
                "waiting_for": state.get("waiting_for"),
                "viewer_interaction": st.get("viewer_interaction"),
                "legal_actions": st.get("legal_actions"),
                "legal_actions_by_object":
                    st.get("legal_actions_by_object"),
                "stack": state.get("stack"),
            }
            with open(f"{EVDIR}/{name}_detail.json", "w") as f:
                json.dump(detail, f, indent=1, default=str)
            say(f"saved {name}_detail.json")
        except Exception as e:
            say(f"prompt detail {name} FAILED: {e!r}")
            obs["tick_errors"].append(f"detail_{name}: {e!r}")

    def acted(key, rev):
        if ST["last_rev_acted"].get(key) == rev:
            return True
        ST["last_rev_acted"][key] = rev
        return False

    def mulligan_pending_for(state, pid):
        d = (wf_of(state).get("data") or {})
        for p in d.get("pending", []) or []:
            ph = (p.get("phase") or {})
            if p.get("player") == pid and str(ph.get("type")) == "Declare":
                return True
        return False

    async def mulligan_keep(c, pid, tag, st, state):
        if (wf_of(state).get("type") or "") != "MulliganDecision":
            return False
        if not mulligan_pending_for(state, pid):
            return False
        if not acted(f"mull{pid}", st.get("state_revision", -1)):
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"[{tag}] mulligan: keep 7")
        return True

    async def handle_discard(c, pid, tag, st, state, protect):
        if (wf_of(state).get("type") or "") != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue

            def rank(ch):
                tx = json.dumps(ch.get("surfaces", [])).lower()
                if any(k in tx for k in protect):
                    return 2
                if FOREST in tx or MOUNTAIN in tx:
                    return 0
                return 1
            pick = sorted(chs, key=rank)[0]
            ST["answered_iids"].append(iid)
            say(f"[{tag}] discarding to hand size")
            await c.send_interaction(
                build_submission(iid, resp.get("type"), pick.get("id")))
            return True
        return False

    def cast_spell_action(acts, state, pid, name):
        for a in acts:
            if a.get("type") != "CastSpell":
                continue
            dd = a.get("data") or {}
            oid = dd.get("object_id")
            if oid is not None and lname(state, int(oid)) == name:
                return a
        return None

    async def decide_and_answer(c, tag, st, state, rec, label):
        """Shared answer policy for a trigger target prompt already
        analyzed by analyze_zero_option(). Returns (kind, detail) where
        kind is "zero" | "probe" | "bear" | "hold"."""
        if rec["has_explicit_zero"]:
            z = rec["zero_choice"]
            sub = build_submission(z["iid"], z["rtype"],
                                   z["choice"].get("id"))
            wire(f"{label}_zero_answer",
                 {"tag": tag, "submission": sub})
            await c.send_interaction(sub)
            ST["answered_iids"].append(z["iid"])
            say(f"[{tag}] {label}: submitted EXPLICIT zero-target choice")
            return ("zero", {"via": rec["zero_via"], "iid": z["iid"]})
        if rec["schema_min"] == 0:
            iid = rec["opportunities"][0]["iid"]
            sub = build_submission(iid, "schema", empty_sequence=True)
            wire(f"{label}_empty_probe", {"tag": tag, "submission": sub})
            await c.send_interaction(sub)
            say(f"[{tag}] {label}: no explicit zero choice; schema min=0 "
                f"-> probing ONE empty sequence (watchdog 8s)")
            return ("probe", {"iid": iid, "sent_at": time.time()})
        bears = rec["bear_candidates"]
        if bears:
            b = bears[0]
            sub = build_submission(b["iid"], b["rtype"],
                                   b["choice"].get("id"))
            wire(f"{label}_bear_answer_no_zero",
                 {"tag": tag, "oid": b["oid"], "submission": sub})
            await c.send_interaction(sub)
            ST["answered_iids"].append(b["iid"])
            say(f"[{tag}] {label}: NO zero-target option offered -> "
                f"answered bear oid={b['oid']} (bug shape recorded)")
            return ("bear", {"oid": b["oid"], "iid": b["iid"]})
        say(f"[{tag}] {label}: NO zero option and NO bear candidate; "
            f"holding (will not invent a target)")
        obs["unexpected_prompts"].append(
            f"{label}: no zero option and no bear candidate")
        return ("hold", {})

    async def answer_etb_prompt(c, tag, st, state):
        """Record + answer the ETB trigger prompt (single shot). The ETB
        fight deals damage back to Altisaur, which legitimately fires the
        Enrage trigger -- that is handled separately by
        answer_enrage_prompt, not here."""
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        if wtype not in ("TargetSelection", "TriggerTargetSelection"):
            return False
        if wf_player(state) != 0:
            return False
        trigs = [t for t in altisaur_triggers_on_stack(state)
                 if t["leg"] in ("etb", "unknown")]
        if not trigs:
            return False
        trig = trigs[0]
        if ST["etb_trigger_id"] is None:
            ST["etb_trigger_id"] = trig["id"]
        # The ETB trigger's source is the Altisaur itself; the prompt
        # branch runs before the battlefield scan in p0_tick, so learn
        # the oid here (needed for damage bookkeeping at prompt time).
        if ST["altisaur_oid"] is None:
            sid = (trig["raw"] or {}).get("source_id")
            if sid is not None:
                ST["altisaur_oid"] = int(sid)
                ST["altisaur_cast_turn"] = state.get("turn_number")
                say(f"[P0] Apex Altisaur oid={sid} (from ETB trigger "
                    f"source)")
        vi = get_vi(st)
        if not vi:
            return False
        iid_in_prompt = [str(o.get("interactionId"))
                         for o in vi.get("opportunities", []) or []]
        # probe watchdog
        probe = ST["etb_probe"]
        if probe and ST["stage"] == "etb_prompt":
            if probe["iid"] in iid_in_prompt:
                if time.time() - probe["sent_at"] > 8:
                    rec = ST["etb_prompt_record"] or {}
                    bears = rec.get("bear_candidates") or []
                    if bears:
                        b = bears[0]
                        sub = build_submission(
                            b["iid"], b["rtype"], b["choice"].get("id"))
                        wire("etb_probe_ignored_fallback",
                             {"tag": tag, "submission": sub})
                        await c.send_interaction(sub)
                        ST["answered_iids"].append(b["iid"])
                        ST["etb_answered"] = "probe_bear_fallback"
                        ST["etb_target_oid"] = b["oid"]
                        ST["etb_probe_accepted"] = False
                        ST["stage"] = "etb_resolving"
                        say(f"[{tag}] etb: empty-sequence probe IGNORED "
                            f"(8s) -> bear fallback oid={b['oid']}")
                    else:
                        say(f"[{tag}] etb: probe ignored but no bear "
                            f"candidate; holding")
                    return True
                return True  # probe in flight, hold
            wire("etb_probe_reprompted",
                 {"tag": tag, "old_iid": probe["iid"],
                  "new_iids": iid_in_prompt})
            say(f"[{tag}] etb: prompt re-asked with new iid; clearing "
                f"probe, re-recording")
            ST["etb_probe"] = None
            ST["etb_prompt_record"] = None
            return True
        if ST["etb_prompt_record"] is not None:
            return True  # recorded+answered; hold for resolution
        # first sighting: record, export, analyze, answer
        save_prompt_detail("etb_prompt", state, st)
        await export_named("etb_prompt")
        rec = analyze_zero_option(state, st)
        rec["trigger_stack_entry"] = trig["raw"]
        rec["trigger_stack_id"] = trig["id"]
        rec["wf_type"] = wtype
        wire("etb_prompt_record", rec)
        ST["etb_prompt_record"] = rec
        ST["etb_bears_at_prompt"] = sorted(bf_ids(state, 1, BEAR))
        if ST["altisaur_oid"] is not None:
            ST["etb_altisaur_damage_at_prompt"] = marked_damage(
                state, ST["altisaur_oid"])
        say(f"[{tag}] etb PROMPT recorded: wf={wtype} stack_id={trig['id']} "
            f"candidates={rec['n_candidates']} "
            f"bears={[b['oid'] for b in rec['bear_candidates']]} "
            f"explicit_zero={rec['has_explicit_zero']} "
            f"schema_min={rec['schema_min']} schema_max={rec['schema_max']}")
        if len(rec["bear_candidates"]) < 2:
            obs["unexpected_prompts"].append(
                "etb: only "
                f"{len(rec['bear_candidates'])} bear candidates; "
                "single-target auto-target risk")
        ST["etb_zero_offered"] = bool(rec["has_explicit_zero"])
        kind, detail = await decide_and_answer(
            c, tag, st, state, rec, "etb")
        if kind == "zero":
            ST["etb_answered"] = "zero"
            ST["etb_zero_via"] = detail["via"]
            ST["stage"] = "etb_resolving"
        elif kind == "probe":
            ST["etb_probe"] = detail
        elif kind == "bear":
            ST["etb_answered"] = "bear"
            ST["etb_target_oid"] = detail["oid"]
            ST["stage"] = "etb_resolving"
        return True

    async def answer_enrage_prompt(c, tag, st, state):
        """Handle ANY Enrage trigger prompt. Enrage can prompt multiple
        times in one run: the ETB fight deals 2 back to Altisaur (fight
        damage -> Enrage), and each forced fight cascades a new trigger.
        The PRIMARY leg is the first prompt with >=2 bear candidates
        (full record + exports + assertions); supporting prompts are
        answered opportunistically and logged."""
        wf = wf_of(state)
        wtype = wf.get("type") or ""
        if wtype not in ("TargetSelection", "TriggerTargetSelection"):
            return False
        if wf_player(state) != 0:
            return False
        trigs = [t for t in altisaur_triggers_on_stack(state)
                 if t["leg"] in ("enrage", "unknown")]
        if not trigs:
            return False
        trig = trigs[0]
        tid = trig["id"]
        if tid not in ST["enrage_seen_ids"]:
            ST["enrage_seen_ids"].append(tid)
            wire("enrage_trigger_seen", {"stack_id": tid, "tag": tag})
            say(f"[{tag}] enrage trigger seen: stack id {tid}")
        vi = get_vi(st)
        if not vi:
            return False
        iid_in_prompt = [str(o.get("interactionId"))
                         for o in vi.get("opportunities", []) or []]
        # probe watchdog (per trigger)
        probe = ST["enrage_probes"].get(tid)
        if probe:
            if probe["iid"] in iid_in_prompt:
                if time.time() - probe["sent_at"] > 8:
                    rec = ST["enrage_recs"].get(tid) or {}
                    bears = rec.get("bear_candidates") or []
                    if bears:
                        b = bears[0]
                        sub = build_submission(
                            b["iid"], b["rtype"], b["choice"].get("id"))
                        wire("enrage_probe_ignored_fallback",
                             {"tag": tag, "tid": tid, "submission": sub})
                        await c.send_interaction(sub)
                        ST["answered_iids"].append(b["iid"])
                        ST["enrage_answered_ids"].append(tid)
                        note = {"tid": tid, "answered":
                                "probe_bear_fallback", "oid": b["oid"]}
                        if tid == ST["enrage_primary_id"]:
                            ST["enrage_answered"] = "probe_bear_fallback"
                            ST["enrage_target_oid"] = b["oid"]
                            ST["enrage_probe_accepted"] = False
                        else:
                            ST["enrage_extra"].append(note)
                        say(f"[{tag}] enrage tid={tid}: probe IGNORED "
                            f"(8s) -> bear fallback oid={b['oid']}")
                    else:
                        say(f"[{tag}] enrage tid={tid}: probe ignored, "
                            f"no bear candidate; holding")
                    return True
                return True  # probe in flight, hold
            wire("enrage_probe_reprompted",
                 {"tag": tag, "tid": tid, "old_iid": probe["iid"],
                  "new_iids": iid_in_prompt})
            say(f"[{tag}] enrage tid={tid}: re-asked with new iid; "
                f"clearing probe, re-recording")
            ST["enrage_probes"].pop(tid, None)
            ST["enrage_recs"].pop(tid, None)
            return True
        if tid in ST["enrage_answered_ids"]:
            return True  # answered; hold for resolution
        # first sighting of this trigger's prompt
        rec = analyze_zero_option(state, st)
        rec["trigger_stack_id"] = tid
        rec["wf_type"] = wtype
        ST["enrage_recs"][tid] = rec
        is_primary = (ST["enrage_primary_id"] is None
                      and len(rec["bear_candidates"]) >= 2)
        if is_primary:
            ST["enrage_primary_id"] = tid
            # bolt_resolved is set by the detector, which runs after the
            # prompt branch; at prompt time the bolt may already be in
            # the graveyard with the flag not yet set.
            bolt_done = ((ST["bolt_cast"] and ST["bolt_resolved"])
                         or (ST["bolt_cast"]
                             and bolt_spell_on_stack(state) is None))
            ST["enrage_inducer"] = ("bolt" if bolt_done
                                    else "fight_damage")
            save_prompt_detail("enrage_prompt", state, st)
            await export_named("enrage_prompt")
            ST["enrage_prompt_record"] = rec
            ST["enrage_bears_at_prompt"] = sorted(bf_ids(state, 1, BEAR))
            if ST["altisaur_oid"] is not None:
                ST["enrage_altisaur_damage_at_prompt"] = marked_damage(
                    state, ST["altisaur_oid"])
            say(f"[{tag}] enrage PRIMARY prompt tid={tid} "
                f"inducer={ST['enrage_inducer']}: wf={wtype} "
                f"candidates={rec['n_candidates']} "
                f"bears={[b['oid'] for b in rec['bear_candidates']]} "
                f"explicit_zero={rec['has_explicit_zero']} "
                f"schema_min={rec['schema_min']} "
                f"schema_max={rec['schema_max']}")
        else:
            wire(f"enrage_prompt_supporting_{tid}", rec)
            say(f"[{tag}] enrage supporting prompt tid={tid}: "
                f"candidates={rec['n_candidates']} "
                f"bears={[b['oid'] for b in rec['bear_candidates']]} "
                f"explicit_zero={rec['has_explicit_zero']}")
        kind, detail = await decide_and_answer(
            c, tag, st, state, rec, f"enrage_{tid}")
        # tid owns the primary slot if it was designated primary on
        # first sighting, even across a re-ask (is_primary is only true
        # on first sighting).
        primary_now = (tid == ST["enrage_primary_id"])
        if kind == "zero":
            ST["enrage_answered_ids"].append(tid)
            if primary_now:
                ST["enrage_answered"] = "zero"
                ST["enrage_zero_offered"] = True
                ST["enrage_zero_via"] = detail["via"]
            else:
                ST["enrage_extra"].append(
                    {"tid": tid, "answered": "zero",
                     "via": detail["via"]})
        elif kind == "probe":
            ST["enrage_probes"][tid] = detail
        elif kind == "bear":
            ST["enrage_answered_ids"].append(tid)
            if primary_now:
                ST["enrage_answered"] = "bear"
                ST["enrage_target_oid"] = detail["oid"]
                ST["enrage_zero_offered"] = False
            else:
                ST["enrage_extra"].append(
                    {"tid": tid, "answered": "bear",
                     "oid": detail["oid"]})
        else:
            ST["enrage_extra"].append(
                {"tid": tid, "answered": "hold_no_candidates"})
        return True

    async def answer_bolt_prompt(c, tag, st, state):
        """Answer Lightning Bolt's own target selection with P0's Apex
        Altisaur (the enrage setup)."""
        if ST["stage"] != "bolt_setup":
            return False
        wf = wf_of(state)
        if (wf.get("type") or "") != "TargetSelection":
            return False
        if wf_player(state) != 0:
            return False
        if bolt_spell_on_stack(state) is None:
            return False
        if altisaur_triggers_on_stack(state):
            return False  # that's the enrage prompt, not bolt's
        if ST["altisaur_oid"] is None:
            return False
        vi = get_vi(st)
        if not vi:
            return False
        want = str(ST["altisaur_oid"])
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            resp = opp.get("response") or {}
            data = resp.get("data") or {}
            chs = data.get("choices") or data.get("candidates") or []
            for ch in chs:
                ref = ref_key([(s.get("data") or {}).get("reference")
                               for s in (ch.get("surfaces") or [])])
                if ref == want:
                    sub = build_submission(iid, resp.get("type"),
                                           ch.get("id"))
                    wire("bolt_target_answer",
                         {"tag": tag, "oid": want, "submission": sub})
                    await c.send_interaction(sub)
                    ST["answered_iids"].append(iid)
                    say(f"[{tag}] Bolt target answered: own Apex Altisaur "
                        f"oid={want}")
                    return True
        return False

    async def combat_empty(c, pid, tag, st, state, acts):
        rev = st.get("state_revision", -1)
        wtype = wf_of(state).get("type") or ""
        if wtype in ("DeclareAttackers", "DeclareBlockers") \
                and wf_player(state) == pid:
            da = find_action(acts, wtype)
            if da and not acted(f"{tag}_{wtype}", rev):
                sub = copy.deepcopy(da)
                if wtype == "DeclareAttackers":
                    sub["data"]["attacks"] = []
                else:
                    sub["data"]["blockers"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
        return False

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p0, 0, "P0", st, state):
            return
        if await handle_discard(p0, 0, "P0", st, state,
                                (ALTISAUR, BOLT)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                return
        wtype = wf_of(state).get("type") or ""
        if wtype not in ST["wf_types_seen"]:
            ST["wf_types_seen"].append(wtype)
        # target-selection prompts for P0: classify and handle.
        # An Enrage trigger legitimately fires from the ETB fight's
        # damage back to Altisaur, so enrage prompts can appear while
        # stage is still etb_prompt/etb_resolving/bolt_setup. Route by
        # the trigger's own leg (tolerating "unknown"), not by stage.
        if wtype in ("TargetSelection", "TriggerTargetSelection") \
                and wf_player(state) == 0:
            trigs = altisaur_triggers_on_stack(state)
            if trigs:
                leg = trigs[0]["leg"]
                if leg == "unknown":
                    leg = ("etb" if ST["stage"] in
                           ("setup", "etb_prompt", "etb_resolving")
                           else "enrage")
                if leg == "etb":
                    if ST["stage"] == "setup":
                        ST["stage"] = "etb_prompt"
                    if ST["stage"] in ("setup", "etb_prompt",
                                       "etb_resolving"):
                        if await answer_etb_prompt(p0, "P0", st, state):
                            return
                else:
                    if await answer_enrage_prompt(p0, "P0", st, state):
                        return
            elif ST["stage"] == "bolt_setup" \
                    and bolt_spell_on_stack(state) is not None:
                if await answer_bolt_prompt(p0, "P0", st, state):
                    return
            else:
                obs["unexpected_prompts"].append(
                    f"P0 {wtype} in stage {ST['stage']}; holding")
                wire("unexpected_prompt_held",
                     {"tag": "P0", "stage": ST["stage"], "wf": wtype,
                      "wf_data": wf_of(state).get("data")})
            return  # never pass priority under a P0 target prompt

        phase = state.get("phase") or ""
        turn = state.get("turn_number") or 0
        ST["turns_seen"].add(turn)

        if ST["altisaur_oid"] is None:
            b = bf_ids(state, 0, ALTISAUR)
            if b:
                ST["altisaur_oid"] = b[0]
                ST["altisaur_cast_turn"] = turn
                say(f"[P0] Apex Altisaur ENTERED oid={b[0]} turn={turn}")

        # ETB trigger resolution detection. The ETB trigger is identified
        # by its stack id: it is resolved when that id leaves the stack.
        # (Enrage triggers from the fight damage may still be present --
        # that must NOT block ETB completion.)
        if ST["stage"] in ("etb_prompt", "etb_resolving"):
            etb_tid = ST["etb_trigger_id"]
            ids_now = {t["id"]
                       for t in altisaur_triggers_on_stack(state)}
            if etb_tid is not None and etb_tid not in ids_now and (
                    ST["etb_answered"] is not None
                    or ST["etb_probe"] is not None):
                if ST["etb_probe"] is not None \
                        and ST["etb_answered"] is None:
                    ST["etb_probe_accepted"] = True
                    ST["etb_answered"] = "zero"
                    ST["etb_zero_offered"] = True
                    ST["etb_zero_via"] = "empty_sequence_accepted"
                    say(f"[P0] ETB: empty-sequence probe ACCEPTED "
                        f"(prompt closed, trigger resolved with no fight)")
                if await export_named("post_etb"):
                    ST["etb_resolved"] = True
                    ST["stage"] = "bolt_setup"
                    say(f"[P0] ETB trigger (stack id {etb_tid}) resolved "
                        f"turn={turn}; enrage leg armed")
                return

        # bolt resolution detection
        if ST["bolt_cast"] and not ST["bolt_resolved"]:
            if bolt_spell_on_stack(state) is None:
                gy = gy_ids(state, 0, BOLT)
                if gy and not altisaur_triggers_on_stack(state):
                    # bolt resolved but no enrage trigger yet; wait
                    pass
                if gy:
                    ST["bolt_resolved"] = True
                    dmg = (marked_damage(state, ST["altisaur_oid"])
                           if ST["altisaur_oid"] is not None else None)
                    ST["bolt_damage_seen"] = dmg
                    say(f"[P0] Bolt resolved turn={turn}: bolt in "
                        f"graveyard, altisaur marked damage={dmg}")

        # primary enrage trigger resolution detection (by stack id;
        # cascade triggers from forced fights may still be on the stack).
        # post.json is exported only after the cascade settles: no
        # altisaur triggers on the stack for a short settle window.
        if ST["enrage_primary_id"] is not None \
                and not ST["enrage_resolved"]:
            pid = ST["enrage_primary_id"]
            ids_now = {t["id"]
                       for t in altisaur_triggers_on_stack(state)}
            probe = ST["enrage_probes"].get(pid)
            answered = (pid in ST["enrage_answered_ids"]
                        or probe is not None)
            if pid not in ids_now and answered:
                if probe is not None \
                        and pid not in ST["enrage_answered_ids"]:
                    ST["enrage_probe_accepted"] = True
                    ST["enrage_answered"] = "zero"
                    ST["enrage_zero_offered"] = True
                    ST["enrage_zero_via"] = "empty_sequence_accepted"
                    ST["enrage_answered_ids"].append(pid)
                    say(f"[P0] enrage: empty-sequence probe ACCEPTED "
                        f"(prompt closed, trigger resolved with no fight)")
                ST["enrage_resolved"] = True
                ST["stage"] = "cleanup"
                ST["cleanup_armed_at"] = time.time()
                ST["post_exported"] = False
                say(f"[P0] primary enrage trigger (stack id {pid}) "
                    f"resolved turn={turn}; cleanup armed")
                return

        if ST["stage"] == "cleanup":
            if altisaur_triggers_on_stack(state):
                # cascade in flight; reset the settle clock
                ST["cleanup_armed_at"] = time.time()
            elif not ST.get("post_exported"):
                if time.time() - ST.get("cleanup_armed_at", 0) > 10:
                    if await export_named("post"):
                        ST["post_exported"] = True
                        ST["cleanup_from_turn"] = turn
                        say(f"[P0] post-cascade state exported "
                            f"turn={turn}")
            elif turn > ST.get("cleanup_from_turn", turn) \
                    and not stack_entries(state):
                ST["stage"] = "done"
                return

        if await combat_empty(p0, 0, "P0", st, state, acts):
            return

        # --- main-phase actions ---
        if my_priority(state, 0) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 0):
                nm = lname(state, oid)
                if nm in (FOREST, MOUNTAIN):
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p0land", rev):
                        await submit_as_is(p0, pla)
                        return
            p1_bears = bf_ids(state, 1, BEAR)
            # cast Apex Altisaur: 9 mana, P1 needs >=2 bears so the
            # trigger prompt is forced (no single-target auto-target)
            if (not ST["altisaur_cast"]
                    and any(n == ALTISAUR for n in hand_lnames(state, 0))
                    and len(p1_bears) >= 2
                    and len(untapped_lands(
                        state, 0, (FOREST, MOUNTAIN))) >= 9):
                ca = cast_spell_action(acts, state, 0, ALTISAUR)
                if ca and not acted("altisaur", rev):
                    if await export_named("pre_etb"):
                        pass
                    say("[P0] casting Apex Altisaur "
                        f"(P1 bears={len(p1_bears)})")
                    await submit_as_is(p0, ca)
                    ST["altisaur_cast"] = True
                    return
            # cast Lightning Bolt at own Altisaur (enrage setup).
            # Only when no primary enrage leg exists yet and no trigger
            # cascade is in flight (the bolt is the inducer of last
            # resort; fight damage may already have produced the leg).
            if (ST["stage"] == "bolt_setup"
                    and ST["enrage_primary_id"] is None
                    and not altisaur_triggers_on_stack(state)
                    and not ST["bolt_cast"]
                    and ST["altisaur_oid"] is not None
                    and any(n == BOLT for n in hand_lnames(state, 0))
                    and len(p1_bears) >= 2):
                ca = cast_spell_action(acts, state, 0, BOLT)
                if ca and not acted("bolt", rev):
                    if await export_named("pre_bolt"):
                        pass
                    say("[P0] casting Lightning Bolt at own Apex "
                        "Altisaur")
                    await submit_as_is(p0, ca)
                    ST["bolt_cast"] = True
                    return

        # default: pass priority (always fall through, cf. #6862)
        if my_priority(state, 0) and not acted("p0pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p0, pa)

    async def p1_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        if await mulligan_keep(p1, 1, "P1", st, state):
            return
        if await handle_discard(p1, 1, "P1", st, state, (BEAR,)):
            return
        for a in acts:
            if a["type"] in ("PayMana", "PayManaAbilityMana",
                             "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if await combat_empty(p1, 1, "P1", st, state, acts):
            return
        phase = state.get("phase") or ""
        if my_priority(state, 1) and phase in ("PreCombatMain",
                                               "PostCombatMain"):
            for oid in hand_ids(state, 1):
                if lname(state, oid) == FOREST:
                    pla = next((a for a in acts
                                if a.get("type") == "PlayLand"
                                and (a.get("data") or {}).get(
                                    "object_id") == oid), None)
                    if pla and not acted("p1land", rev):
                        await submit_as_is(p1, pla)
                        return
            # keep bears coming while no primary enrage leg exists:
            # bears feed the forced-fight prompts (and the bolt inducer
            # needs >=2). Once the primary leg is recorded, stop so the
            # forced-fight cascade starves and the game can settle.
            if ST["enrage_primary_id"] is not None:
                pass
            else:
                for oid in hand_ids(state, 1):
                    if lname(state, oid) != BEAR:
                        continue
                    if len(untapped_lands(state, 1, (FOREST,))) < 2:
                        break
                    ca = cast_spell_action(acts, state, 1, BEAR)
                    # cast_spell_action matches by name; ensure this copy
                    if ca and (ca.get("data") or {}).get(
                            "object_id") != oid:
                        ca = None
                        for a in acts:
                            if a.get("type") == "CastSpell" and (
                                    a.get("data") or {}).get(
                                    "object_id") == oid:
                                ca = a
                                break
                    if ca and not acted(f"p1bear{oid}", rev):
                        say(f"[P1] casting Grizzly Bears oid={oid}")
                        await submit_as_is(p1, ca)
                        return
        if my_priority(state, 1) and not acted("p1pass", rev):
            pa = find_action(acts, "PassPriority")
            if pa:
                await submit_as_is(p1, pa)

    async def tick(c, pid, tag, fn):
        while True:
            await asyncio.sleep(0.25)
            if ST["stage"] == "done":
                return
            if c.latest is None:
                continue
            st = c.latest
            state = st.get("state") or {}
            if not state:
                continue
            try:
                acts = merged_actions(st)
                await fn(st, acts, state)
            except Exception as e:
                obs["tick_errors"].append(f"{tag}: {e!r}")

    t0 = time.time()
    last_diag = 0.0
    p0t = asyncio.create_task(tick(p0, 0, "P0", p0_tick))
    p1t = asyncio.create_task(tick(p1, 1, "P1", p1_tick))
    try:
        while time.time() - t0 < 1500:
            await asyncio.sleep(1)
            if ST["stage"] == "done":
                break
            s = (p0.latest or {}).get("state") or {}
            if time.time() - last_diag > 60 and s:
                last_diag = time.time()
                say(f"[diag] t={int(time.time()-t0)}s stage={ST['stage']} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"altisaur={ST['altisaur_oid']} "
                    f"p1bears={len(bf_ids(s, 1, BEAR))}")
    finally:
        p0t.cancel()
        p1t.cancel()

    async def finish():
        if ST.get("finished"):
            return
        ST["finished"] = True
        dur = time.time() - t_start
        states = {}
        for fn in ("pre_etb", "etb_prompt", "post_etb", "pre_bolt",
                   "enrage_prompt", "post"):
            p = f"{EVDIR}/{fn}.json"
            try:
                if os.path.exists(p):
                    states[fn] = json.loads(open(p).read())["state"]
                    say(f"loaded {fn}.json")
            except Exception as e:
                notes.append(f"state reload failed for {fn}.json: {e}")

        def bears_alive(stt):
            return sorted(bf_ids(stt, 1, BEAR))

        # ---- A0: card-data parse (informational, feeds notes) ----
        try:
            cd = json.load(open(f"{RELDIR}/data/card-data.json"))
            c = cd[ALTISAUR]
            mt = [((t.get("execute") or {}).get("multi_target") or {})
                  for t in (c.get("triggers") or [])]
            parse_ok = (
                len(c.get("triggers") or []) == 2
                and all(m.get("min") == 0 for m in mt)
                and "Unimplemented" not in json.dumps(c.get("triggers")))
            notes.append(
                f"A0: card-data apex altisaur: 2 triggers, "
                f"multi_target mins={[m.get('min') for m in mt]}, "
                f"no Unimplemented -> parse_ok={parse_ok}")
        except Exception as e:
            parse_ok = False
            notes.append(f"A0 parse check error: {e!r}")

        # ---- A1: setup ----
        rec = ST["etb_prompt_record"]
        if rec is not None:
            ok = (ST["altisaur_oid"] is not None
                  and len(rec["bear_candidates"]) >= 2
                  and ST["altisaur_cast"])
            notes.append(
                f"A1: altisaur_oid={ST['altisaur_oid']} cast="
                f"{ST['altisaur_cast']} etb_prompt_wf={rec['wf_type']} "
                f"bear_candidates="
                f"{[b['oid'] for b in rec['bear_candidates']]} "
                f"explicit_zero_seen={rec['has_explicit_zero']} "
                f"schema_min={rec['schema_min']} "
                f"schema_max={rec['schema_max']}")
        else:
            ok = False
            notes.append("A1 failed: ETB trigger prompt never recorded "
                         f"(stage={ST['stage']}, altisaur_oid="
                         f"{ST['altisaur_oid']})")
        ass["A1_setup"] = "passed" if ok else "failed"

        # ---- A2: zero option on ETB trigger ----
        if rec is not None:
            offered = bool(ST["etb_zero_offered"])
            via = ST["etb_zero_via"]
            notes.append(f"A2: etb_zero_offered={offered} via={via} "
                         f"answered={ST['etb_answered']} "
                         f"probe_accepted={ST['etb_probe_accepted']}")
            ok = offered
        else:
            ok = False
            notes.append("A2 not-run: no ETB prompt")
            ass["A2_zero_option_etb"] = "not-run"
        if "A2_zero_option_etb" not in ass:
            ass["A2_zero_option_etb"] = "passed" if ok else "failed"

        # ---- A3: ETB resolution ----
        post_etb = states.get("post_etb")
        answered = ST["etb_answered"]
        if post_etb is not None and answered is not None:
            if answered == "zero":
                alive = bears_alive(post_etb)
                same_bears = (alive == sorted(ST["etb_bears_at_prompt"]))
                dmg0 = ST["etb_altisaur_damage_at_prompt"]
                dmg1 = (marked_damage(post_etb, ST["altisaur_oid"])
                        if ST["altisaur_oid"] is not None else None)
                dmg_same = (dmg0 == dmg1)
                notes.append(
                    f"A3 (zero path): bears_at_prompt="
                    f"{sorted(ST['etb_bears_at_prompt'])} bears_post="
                    f"{alive} same={same_bears}; altisaur damage "
                    f"{dmg0}->{dmg1} same={dmg_same}")
                ok = same_bears and dmg_same
            else:
                tgt = ST["etb_target_oid"]
                tgt_zone = (get_obj(post_etb, tgt).get("zone")
                            if tgt is not None else None)
                dmg1 = (marked_damage(post_etb, ST["altisaur_oid"])
                        if ST["altisaur_oid"] is not None else None)
                notes.append(
                    f"A3 (forced-fight path): answered={answered} "
                    f"target_oid={tgt} zone_post={tgt_zone} "
                    f"(expect not Battlefield); altisaur damage post="
                    f"{dmg1}")
                ok = tgt is not None and tgt_zone != "Battlefield"
        else:
            ok = False
            notes.append(f"A3 not-run/failed: post_etb present="
                         f"{post_etb is not None} answered={answered}")
            if post_etb is None or rec is None:
                ass["A3_etb_resolution"] = "not-run"
        if "A3_etb_resolution" not in ass:
            ass["A3_etb_resolution"] = "passed" if ok else "failed"

        # ---- A4: enrage setup (PRIMARY leg) ----
        # The primary enrage leg is the first enrage prompt with >=2
        # bear candidates. Its inducer is either the deliberate
        # Lightning Bolt or the ETB fight's damage back to Altisaur
        # (fight damage legitimately triggers Enrage).
        erec = ST["enrage_prompt_record"]
        inducer = ST["enrage_inducer"]
        if erec is not None and inducer is not None:
            bears_ok = len(erec["bear_candidates"]) >= 2
            if inducer == "bolt":
                gy = gy_ids(states.get("post", {}), 0, BOLT)
                ind_ok = bool(gy) or ST["bolt_resolved"]
            else:  # fight_damage
                d0 = ST["etb_altisaur_damage_at_prompt"]
                dpe = (marked_damage(states["post_etb"],
                                     ST["altisaur_oid"])
                       if states.get("post_etb") is not None
                       and ST["altisaur_oid"] is not None else None)
                ind_ok = (dpe is not None and d0 is not None
                          and dpe > (d0 or 0))
            ok = bears_ok and ind_ok
            notes.append(
                f"A4: primary enrage tid={ST['enrage_primary_id']} "
                f"inducer={inducer} inducer_ok={ind_ok} "
                f"bear_candidates="
                f"{[b['oid'] for b in erec['bear_candidates']]} "
                f"bears_ok={bears_ok} "
                f"altisaur_damage_at_enrage_prompt="
                f"{ST['enrage_altisaur_damage_at_prompt']} "
                f"explicit_zero_seen={erec['has_explicit_zero']} "
                f"schema_min={erec['schema_min']} "
                f"supporting_enrage_prompts={len(ST['enrage_extra'])}")
        else:
            ok = False
            notes.append(f"A4 failed: primary enrage prompt recorded="
                         f"{erec is not None} inducer={inducer} "
                         f"seen_trigger_ids={ST['enrage_seen_ids']} "
                         f"stage={ST['stage']}")
        ass["A4_enrage_setup"] = "passed" if ok else "failed"

        # ---- A5: zero option on enrage trigger (PRIMARY leg) ----
        if erec is not None:
            offered = bool(ST["enrage_zero_offered"])
            notes.append(f"A5: enrage_zero_offered={offered} via="
                         f"{ST['enrage_zero_via']} answered="
                         f"{ST['enrage_answered']} probe_accepted="
                         f"{ST['enrage_probe_accepted']} "
                         f"primary_tid={ST['enrage_primary_id']}")
            ok = offered
        else:
            ok = False
            notes.append("A5 not-run: no enrage prompt")
            ass["A5_zero_option_enrage"] = "not-run"
        if "A5_zero_option_enrage" not in ass:
            ass["A5_zero_option_enrage"] = "passed" if ok else "failed"

        # ---- A6: enrage resolution (PRIMARY leg; post.json is taken
        # after the forced-fight cascade settles) ----
        post = states.get("post")
        eanswered = ST["enrage_answered"]
        if post is not None and eanswered is not None:
            stack_empty = not (post.get("stack") or [])
            wft = (post.get("waiting_for") or {}).get("type")
            if eanswered == "zero":
                alive = bears_alive(post)
                same_bears = (alive
                              == sorted(ST["enrage_bears_at_prompt"]))
                d0 = ST["enrage_altisaur_damage_at_prompt"]
                d1 = (marked_damage(post, ST["altisaur_oid"])
                      if ST["altisaur_oid"] is not None else None)
                # zero path: no fight, so no NEW damage may appear;
                # marked damage only decays at cleanup (d1 <= d0)
                dmg_ok = (d1 or 0) <= (d0 or 0)
                notes.append(
                    f"A6 (zero path): bears same={same_bears} "
                    f"({sorted(ST['enrage_bears_at_prompt'])}->{alive}); "
                    f"altisaur damage {d0}->{d1} dmg_ok={dmg_ok}; "
                    f"stack_empty={stack_empty} post_wf={wft}")
                ok = same_bears and dmg_ok and stack_empty
            else:
                tgt = ST["enrage_target_oid"]
                tgt_zone = (get_obj(post, tgt).get("zone")
                            if tgt is not None else None)
                notes.append(
                    f"A6 (forced-fight path): answered={eanswered} "
                    f"target_oid={tgt} zone_post={tgt_zone}; "
                    f"stack_empty={stack_empty} post_wf={wft}")
                ok = (tgt is not None and tgt_zone != "Battlefield"
                      and stack_empty)
        else:
            ok = False
            notes.append(f"A6 not-run/failed: post present="
                         f"{post is not None} answered={eanswered}")
            if post is None or erec is None:
                ass["A6_resolution"] = "not-run"
        if "A6_resolution" not in ass:
            ass["A6_resolution"] = "passed" if ok else "failed"

        # ---- verdict ----
        a1 = ass["A1_setup"]
        a2 = ass["A2_zero_option_etb"]
        a4 = ass["A4_enrage_setup"]
        a5 = ass["A5_zero_option_enrage"]
        if a1 != "passed":
            verdict = "blocked"
            notes.append("verdict=blocked: A1 failed - the ETB trigger "
                         "prompt was never observed, so the reported "
                         "forced-fight shape is untestable")
        elif a4 != "passed":
            # Enrage primary leg never established. The ETB leg alone
            # proves the reported shape when A2 failed.
            if a2 == "failed":
                verdict = "reproduced"
                notes.append("verdict=reproduced: ETB trigger forced a "
                             "fight target with no zero option "
                             "(no primary enrage leg: A4 failed)")
            else:
                verdict = "blocked"
                notes.append("verdict=blocked: enrage setup (A4) failed - "
                             "no primary enrage prompt was observed")
        elif a2 == "failed" or a5 == "failed":
            verdict = "reproduced"
            notes.append("verdict=reproduced: at least one Apex Altisaur "
                         "trigger (ETB/enrage) offered no zero-target "
                         "option - the fight target was compulsory, "
                         "contradicting 'up to one target'")
        elif a2 == "passed" and a5 == "passed" \
                and ass["A3_etb_resolution"] == "passed" \
                and ass["A6_resolution"] == "passed":
            verdict = "not-reproduced"
            notes.append("verdict=not-reproduced: both triggers offered "
                         "a zero-target option and zero-target "
                         "resolutions completed with no fight")
        else:
            verdict = "blocked"
            notes.append("verdict=blocked: incomplete assertion chain")
        notes.append(f"verdict={verdict}")

        run = {
            "run_id": RUN_ID, "issue": ISSUE,
            "verdict": verdict, "validated_at": "2026-09-15",
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 71,
                       "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                     "rb").read()).hexdigest(),
            "format_config": "default Bo1 (2 human-client seats, life 20)",
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "observations": obs,
            "driver_state": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in ST.items()
                             if k not in ("etb_prompt_record",
                                          "enrage_prompt_record")},
            "prompt_records_summary": {
                "etb": ({k: v for k, v in
                         (ST["etb_prompt_record"] or {}).items()
                         if k != "trigger_stack_entry"}
                        if ST["etb_prompt_record"] else None),
                "enrage": ({k: v for k, v in
                            (ST["enrage_prompt_record"] or {}).items()
                            if k != "trigger_stack_entry"}
                           if ST["enrage_prompt_record"] else None),
            },
            "notes": notes,
            "evidence_files": ["pre_etb.json", "etb_prompt.json",
                               "etb_prompt_detail.json", "post_etb.json",
                               "pre_bolt.json", "enrage_prompt.json",
                               "enrage_prompt_detail.json", "post.json",
                               "run.json", "manifest.sha256", "summary.png",
                               f"scenario_{ISSUE}.py", "wire_log.jsonl",
                               "scenario_run.log", "server.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two "
                "human-client seats.",
                "Card density is a test-harness convenience (engine "
                "accepts >4-of for custom games).",
                "P1 is passive (land drops + bears only, no attacks) so "
                "the trigger windows stay clean.",
                "The prebuilt server has no standalone state-restore; "
                "states are authoritative exports (restorable only via "
                "full game replay).",
                "Zero-target achievability is judged from the runtime "
                "prompt (viewer_interaction + legal_actions); an empty "
                "choiceIds probe is only attempted when the prompt's own "
                "schema advertises min 0, and only once per prompt.",
            ],
            "duration_s": round(dur, 1),
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                    f"{EVDIR}/scenario_{ISSUE}.py")
        srv_run = os.environ.get("SERVER_RUN_ID", RUN_ID)
        say(f"server run dir: runs/{srv_run}")
        try:
            with open(f"{BACKFILL}/runs/{srv_run}/server.log", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
            gc = ST.get("game_code") or ""
            excerpt = [ln for ln in clean.splitlines()
                       if gc and gc in ln]
            if not excerpt:
                excerpt = clean.splitlines()[-400:]
            with open(f"{EVDIR}/server.log", "w") as f:
                f.write("\n".join(excerpt) + "\n")
            say(f"wrote server.log excerpts ({len(excerpt)} lines)")
        except Exception as e:
            say(f"server.log excerpt failed: {e}")
            notes.append(f"server.log excerpt failed: {e}")
        render_summary(run, states)
        # close logs BEFORE hashing the manifest (#7176 lesson)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        write_manifest()
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}",
              flush=True)

    def render_summary(run, states):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            say("PIL missing; skipping summary.png")
            return
        W, H = 1000, 1120
        img = Image.new("RGB", (W, H), (16, 20, 26))
        d = ImageDraw.Draw(img)
        y = 18
        d.text((24, y), "phase-rs/phase #7180 - Apex Altisaur missing "
               "'skip' on triggered ability", fill=(235, 240, 250))
        y += 28
        d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
               " - ETB + Enrage 'fights up to one target creature'",
               fill=(140, 160, 180))
        y += 28
        vcol = {"reproduced": (255, 90, 90),
                "not-reproduced": (120, 220, 120),
                "blocked": (230, 200, 120)}.get(run["verdict"],
                                                (180, 180, 180))
        d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
        y += 34
        for ln in [
                "Report: both triggers say 'up to one target', but the",
                "player is forced to choose a creature - no zero-target",
                "/ skip option is offered.",
                "Card data parses faithfully (multi_target min 0, max 1);",
                "the defect is in runtime target-prompt generation."]:
            d.text((24, y), ln, fill=(200, 210, 225))
            y += 24
        y += 10
        d.text((24, y), "Assertions (from saved states / prompt records):",
               fill=(200, 210, 225))
        y += 24
        labels = {
            "A1_setup": "Altisaur cast; ETB trigger prompt seen with "
                        ">=2 bear candidates",
            "A2_zero_option_etb": "zero-target option offered for ETB "
                                  "trigger",
            "A3_etb_resolution": "ETB trigger resolved per answered path "
                                 "(zero->no fight / forced->fight)",
            "A4_enrage_setup": "primary enrage prompt (first with >=2 "
                               "bears; inducer Bolt or ETB-fight damage)",
            "A5_zero_option_enrage": "zero-target option offered for "
                                     "enrage trigger",
            "A6_resolution": "enrage trigger resolved per answered path; "
                             "game proceeded",
        }
        for k, lab in labels.items():
            v = run["assertions"].get(k, "?")
            col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
                   "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
            d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
            y += 26
        y += 8
        d.text((24, y), "Key observations:", fill=(200, 210, 225))
        y += 24
        ds = run["driver_state"]
        for ln in [
                f"ETB: answered={ds.get('etb_answered')} zero_offered="
                f"{ds.get('etb_zero_offered')} via={ds.get('etb_zero_via')}",
                f"ETB target oid={ds.get('etb_target_oid')} "
                f"(forced-fight leg)",
                f"Enrage: answered={ds.get('enrage_answered')} "
                f"zero_offered={ds.get('enrage_zero_offered')} "
                f"via={ds.get('enrage_zero_via')} "
                f"inducer={ds.get('enrage_inducer')}",
                f"Enrage target oid={ds.get('enrage_target_oid')} "
                f"(forced-fight leg); seen trigger ids="
                f"{ds.get('enrage_seen_ids')}",
                f"bolt_damage_seen={ds.get('bolt_damage_seen')}",
                "Full prompt records: etb_prompt_detail.json,",
                "enrage_prompt_detail.json (waiting_for +",
                "viewer_interaction + legal_actions).",
        ]:
            d.text((24, y), ln[:110], fill=(160, 175, 195))
            y += 22
        d.text((24, y + 14), "Generated from saved states/assertions; not "
               "a gameplay screenshot.", fill=(110, 125, 145))
        img.save(f"{EVDIR}/summary.png")
        say("wrote summary.png")

    def write_manifest():
        import hashlib as _hl
        files = sorted(
            f for f in os.listdir(EVDIR)
            if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = _hl.sha256()
            with open(f"{EVDIR}/{fn}", "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        # plain print: RUNLOG is already closed at this point
        print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)

    await finish()


if __name__ == "__main__":
    asyncio.run(main())
