#!/usr/bin/env python3
"""Issue #6912: Chaos Warp may have a glitch when targeting commanders.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-02): "Chaos Warp may have a glitch when targeting
commanders?" No concrete symptom was described (classifier: cannot_determine).

Oracle text (pinned card-data.json, 'chaos warp'):
  "The owner of target permanent shuffles it into their library, then
   reveals the top card of their library. If it's a permanent card, they
   put it onto the battlefield."
Parsed (v0.81.3 card-data): ChangeZone(target permanent -> owner's Library,
owner_library=true) ; Shuffle(ParentTargetOwner) ; RevealTop(ParentTargetOwner,1)
; ChangeZone(ParentTarget -> Battlefield, if TargetMatchesFilter Permanent).

Rules expectation (CR 903.9 / 903.9a): if a commander would be put into its
owner's library from anywhere, its owner may put it into the command zone
instead. The replacement is offered to the OWNER of the commander. Chaos
Warp's later instructions ("then" shuffle/reveal/put) still use the target
OWNER's library and must resolve even when the commander moves to the
command zone.

Setup (native engine, two human-client seats, CommanderDraft):
  P0: commander=[zurgo bellstriker] (never cast; sits in CZ), main = 12x
      Chaos Warp + 48x Mountain. P0 casts Chaos Warp targeting P1's commander.
  P1: commander=[ayula, queen among bears] ({1}{G} 2/2, inert here), main =
      60x Forest. P1 casts Ayula, then answers the commander-replacement
      choice as the commander OWNER.

Two runs: CZ_CHOICE=zone   -> P1 moves Ayula to the command zone.
           CZ_CHOICE=library -> P1 lets Ayula shuffle into P1's library.

Expected (per card text + CR 903.9a):
  E1: Ayula on P1's battlefield with is_commander=true; Chaos Warp cast by
      P0 with Ayula as the chosen target.
  E2: a commander-replacement choice is offered to P1 (the owner, chooser=1),
      not to P0.
  E3: zone branch: Ayula ends in the CommandZone; library branch: Ayula ends
      in P1's Library (unless revealed and put onto the battlefield, which is
      rules-correct).
  E4: P1's library is shuffled and its top card revealed; a revealed
      permanent is put onto the battlefield under P1's control; a revealed
      non-permanent stays on top of P1's library. P0's library is untouched.

Assertions:
  A1_setup_ok       pre.json at P0 PreCombatMain: Ayula on P1 BF with
                    is_commander=true, Chaos Warp in P0 hand, 3+ untapped
                    Mountains, both players at 40 life
  A2_cast_targeted  Chaos Warp was cast and the submitted target candidate
                    resolved to Ayula's battlefield oid
  A3_replacement_offered  a commander replacement choice was offered with
                    chooser=1 (the commander owner); offered to P0 counts as
                    FAILED (wrong player)
  A4_zone_correct   zone branch: Ayula zone == CommandZone;
                    library branch: Ayula zone == Library, or Battlefield when
                    the reveal put the shuffled-in Ayula onto the BF
  A5_owner_routing  the shuffle/reveal/put used P1's (owner's) library: a
                    revealed permanent ended on P1's BF with P1's library
                    shrunk by exactly that card; a revealed non-permanent left
                    P1's library size consistent and nothing new on P1's BF
  A6_cleanup        post.json: stack empty, game proceeding (no stall)

Verdict rule: reproduced iff A1 passed and (A3 failed or A4 failed or A5
failed) - the reported commander-targeting glitch. not-reproduced iff
A1..A6 pass. blocked iff the game cannot be driven to the warp resolution.

Evidence: evidence/6912/<run-id>/pre.json, mid.json (right after the
replacement answer), post.json, run.json, manifest.sha256, summary.png,
scenario_6912.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "20260912-6912a")
CZ_CHOICE = os.environ.get("CZ_CHOICE", "zone")  # "zone" | "library"
assert CZ_CHOICE in ("zone", "library"), f"bad CZ_CHOICE={CZ_CHOICE}"
EVDIR = f"{BACKFILL}/evidence/6912/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "wire_log.jsonl")]
assert not leftovers, f"EVDIR not empty: {EVDIR}: {leftovers}"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

WARP = "chaos warp"
MOUNTAIN = "mountain"
FOREST = "forest"
AYULA = "ayula, queen among bears"
ZURGO = "zurgo bellstriker"

P0_COMMANDER = [ZURGO]
P0_MAIN = [(WARP, 12), (MOUNTAIN, 48)]
P1_COMMANDER = [AYULA]
P1_DECK = [(FOREST, 60)]
P2_DECK = [(FOREST, 60)]

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

STARTING_LIFE = 40

SERVER_IDENTITY = {
    "server_version": "0.81.3",
    "build_commit": "95bec6e",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "2c9918612e8fcf35d7daf5964eadaf11eeb94cc99463b2de822a906b9030fa44",
    "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
    "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-12",
    "source": "ServerHello (0.81.3/95bec6e/proto 70/Full) + minisign-verify "
              "(repo-pinned key) of binary + signed data manifest; binary "
              "sha256 matches GitHub asset digest; data files sha256-verified "
              "against manifest. Reused the already-listening pinned server on "
              "127.0.0.1:9374 (started for the #6911 run; same pin).",
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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []))


def lib_top_name(state, pid):
    lib = player_of(state, pid).get("library", []) or []
    return lname(state, lib[-1]) if lib else None


def bf_ids(state, pid, key=None):
    return [int(oid) for oid, o in (state.get("objects", {}) or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (key is None or lname(state, oid) == key.lower())]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


def untapped_lands(state, pid, key=None):
    out = []
    for oid, o in (state.get("objects", {}) or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and nm in (FOREST, MOUNTAIN)):
            if key is None or nm == key:
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


def stack_has_warp(state):
    for e in state.get("stack", []) or []:
        if "chaos warp" in json.dumps(e, default=str).lower():
            return True
    return False


def ref_oid_of_candidate(ch):
    """Extract the targeted object oid from a target candidate's surfaces."""
    found = []

    def rec(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "reference" and isinstance(v, int):
                    found.append(v)
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)

    for s in ch.get("surfaces", []) or []:
        rec(s.get("data"))
    return found[0] if found else None


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
           ("A1_setup_ok", "A2_cast_targeted", "A3_replacement_offered",
            "A4_zone_correct", "A5_owner_routing", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(cdeck(P0_COMMANDER, *P0_MAIN), player_count=3,
                    format_config=COMMANDER_FORMAT)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, cdeck(P1_COMMANDER, *P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id} RUN_ID={RUN_ID} CZ_CHOICE={CZ_CHOICE}")
    kept = {}

    ST = {"warp_cast": False, "warp_target_oid": None, "target_answered": False,
          "warp_cast_at": None, "repl_answered": False, "repl_answered_at": None,
          "pre_ayula_oid": None, "frozen": False, "post_at": None}
    obs = {"repl_seen": None, "repl_chooser": None, "repl_choices": [],
           "repl_chosen": None, "repl_guess": None, "repl_chooser_wrong": False,
           "reveal_prompts": [], "wf_types_resolution": set(),
           "auto_answered": [], "cast_turn": None, "resolve_turn": None,
           "rejections": []}
    pre_exported = False
    mid_exported = False
    post_exported = False
    last_select = {}
    prompt_first_seen = {}

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                notes.append("post.json exported at finish() fallback")
            except Exception as e:
                notes.append(f"post export failed: {e}")
        pre_st = post_st = mid_st = None
        try:
            if os.path.exists(f"{EVDIR}/pre.json"):
                pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
            if os.path.exists(f"{EVDIR}/post.json"):
                post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"]
            if os.path.exists(f"{EVDIR}/mid.json"):
                mid_st = json.loads(open(f"{EVDIR}/mid.json").read())["state"]
        except Exception as e:
            notes.append(f"state reload failed: {e}")

        # ---- A1: setup ----
        if pre_st is not None:
            ay = bf_id(pre_st, 1, AYULA)
            ok = (ay is not None
                  and bool(get_obj(pre_st, ay).get("is_commander"))
                  and WARP in hand_lnames(pre_st, 0)
                  and len(untapped_lands(pre_st, 0, MOUNTAIN)) >= 3
                  and life_of(pre_st, 0) == STARTING_LIFE
                  and life_of(pre_st, 1) == STARTING_LIFE)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: ayula_bf={ay} is_commander="
                         f"{bool(ay is not None and get_obj(pre_st, ay).get('is_commander'))} "
                         f"warp_in_hand={WARP in hand_lnames(pre_st, 0)} "
                         f"untapped_mtn={len(untapped_lands(pre_st, 0, MOUNTAIN))} "
                         f"life={[life_of(pre_st, 0), life_of(pre_st, 1)]}")
            ST["pre_ayula_oid"] = ay
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 failed: pre.json missing")

        # ---- A2: warp cast with Ayula as target ----
        if ST["warp_target_oid"] is not None and ST["pre_ayula_oid"] is not None:
            if ST["warp_target_oid"] == ST["pre_ayula_oid"]:
                ass["A2_cast_targeted"] = "passed"
                notes.append(f"A2 passed: submitted target oid "
                             f"{ST['warp_target_oid']} == pre-cast Ayula oid")
            else:
                ass["A2_cast_targeted"] = "failed"
                notes.append(f"A2 failed: target oid {ST['warp_target_oid']} != "
                             f"Ayula oid {ST['pre_ayula_oid']}")
        else:
            ass["A2_cast_targeted"] = "failed"
            notes.append(f"A2 failed: warp_target_oid={ST['warp_target_oid']} "
                         f"pre_ayula_oid={ST['pre_ayula_oid']}")

        # ---- A3: replacement offered to the owner (P1) ----
        if obs["repl_seen"] is not None:
            if obs["repl_chooser"] == 1:
                ass["A3_replacement_offered"] = "passed"
                notes.append(f"A3 passed: ReplacementChoice offered to P1 "
                             f"(owner), choices={obs['repl_choices']}, "
                             f"chosen={obs['repl_chosen']}"
                             + (f" [{obs['repl_guess']}]" if obs["repl_guess"] else ""))
            else:
                ass["A3_replacement_offered"] = "failed"
                notes.append(f"A3 FAILED: replacement choice offered to player "
                             f"{obs['repl_chooser']} (expected owner P1)")
        else:
            ass["A3_replacement_offered"] = "failed"
            notes.append("A3 FAILED: no commander replacement choice was "
                         "observed during Chaos Warp resolution")

        # ---- A4/A5/A6 from post ----
        if post_st is not None and pre_st is not None:
            ay = ST["pre_ayula_oid"]
            post_zone = zone_of(post_st, ay) if ay is not None else None
            p1_lib_pre = lib_count(pre_st, 1)
            p1_lib_post = lib_count(post_st, 1)
            p0_lib_pre = lib_count(pre_st, 0)
            p0_lib_post = lib_count(post_st, 0)
            p1_bf_pre = set(bf_ids(pre_st, 1))
            p1_bf_post = set(bf_ids(post_st, 1))
            p0_bf_pre = set(bf_ids(pre_st, 0))
            p0_bf_post = set(bf_ids(post_st, 0))
            new_on_p1_bf = [o for o in (p1_bf_post - p1_bf_pre)]
            new_names = sorted(lname(post_st, o) for o in new_on_p1_bf)
            new_on_p0_bf = [o for o in (p0_bf_post - p0_bf_pre)]
            new_p0_names = sorted(lname(post_st, o) for o in new_on_p0_bf)
            new_forests_p1 = [o for o in new_on_p1_bf
                              if lname(post_st, o) == FOREST]
            notes.append(f"post: ayula_zone={post_zone} p1_lib {p1_lib_pre}->{p1_lib_post} "
                         f"p0_lib {p0_lib_pre}->{p0_lib_post} new_on_p1_bf={new_names} "
                         f"new_on_p0_bf={new_p0_names} "
                         f"p1_lib_top={lib_top_name(post_st, 1)}")
            # P1's natural draws between pre and post (frozen board: hand only
            # grows by draws; graveyard must be unchanged).
            p1_hand_pre = len(player_of(pre_st, 1).get("hand", []))
            p1_hand_post = len(player_of(post_st, 1).get("hand", []))
            p1_gy_pre = len(player_of(pre_st, 1).get("graveyard", []))
            p1_gy_post = len(player_of(post_st, 1).get("graveyard", []))
            p1_draws = p1_hand_post - p1_hand_pre
            draws_sane = (p1_gy_post == p1_gy_pre and p1_draws >= 0)
            # A4
            if CZ_CHOICE == "zone":
                if post_zone in ("CommandZone", "Command"):
                    ass["A4_zone_correct"] = "passed"
                    notes.append("A4 passed: Ayula in the command zone after "
                                 "choosing the command-zone replacement")
                else:
                    ass["A4_zone_correct"] = "failed"
                    notes.append(f"A4 FAILED: expected command zone, got {post_zone}")
            else:
                if post_zone == "Library":
                    ass["A4_zone_correct"] = "passed"
                    notes.append("A4 passed: Ayula in P1's Library after "
                                 "declining the command-zone replacement")
                elif (post_zone == "Battlefield"
                        and p1_lib_post == p1_lib_pre - p1_draws
                        and ay in p1_bf_post):
                    ass["A4_zone_correct"] = "passed"
                    notes.append("A4 passed (rules-correct reveal): Ayula was "
                                 "shuffled into P1's library, revealed as the "
                                 "top card, and put onto the battlefield")
                else:
                    ass["A4_zone_correct"] = "failed"
                    notes.append(f"A4 FAILED: expected Library (or revealed "
                                 f"onto BF), got {post_zone}")
            # A5: owner routing. The board is frozen from the replacement
            # answer on, so P0's battlefield must be identical pre->post and
            # P1's library must account for exactly the warp's shuffle+reveal,
            # plus P1's natural draws (computed above).
            # (P1's library is all Forests [+ Ayula in the library branch], so
            # the revealed card is always a permanent and must be put on BF.)
            notes.append(f"A5 accounting: p1 hand {p1_hand_pre}->{p1_hand_post} "
                         f"(draws~{p1_draws}), gy {p1_gy_pre}->{p1_gy_post}")
            if CZ_CHOICE == "zone":
                # commander never entered P1's library: reveal Forest -> BF
                exp_lib = p1_lib_pre - p1_draws - 1
                if (draws_sane and len(new_on_p0_bf) == 0
                        and len(new_forests_p1) == 1
                        and len(new_on_p1_bf) == 1
                        and p1_lib_post == exp_lib):
                    a5 = ("passed", "revealed Forest put onto P1's BF "
                          f"(P1 library {p1_lib_pre}->{p1_lib_post} = "
                          f"-{p1_draws} draw(s) -1 revealed); P0's "
                          "battlefield untouched")
                else:
                    a5 = ("failed", f"unexpected routing: new_on_p1_bf={new_names} "
                          f"new_on_p0_bf={new_p0_names} "
                          f"p1_lib {p1_lib_pre}->{p1_lib_post} (expected "
                          f"{exp_lib}), draws_sane={draws_sane}")
            else:
                # commander shuffled in (+1); revealed permanent put on BF (-1)
                exp_lib = p1_lib_pre - p1_draws
                ayula_back = (post_zone == "Battlefield" and ay in p1_bf_post)
                revealed_ok = ((len(new_forests_p1) == 1
                                and len(new_on_p1_bf) == 1) or ayula_back)
                if (draws_sane and len(new_on_p0_bf) == 0
                        and p1_lib_post == exp_lib and revealed_ok):
                    what = ("revealed Forest put onto P1's BF"
                            if new_forests_p1 else
                            "revealed Ayula put back onto P1's BF")
                    a5 = ("passed", f"{what} (shuffle +1 / put -1 nets 0: P1 "
                          f"library {p1_lib_pre}->{p1_lib_post} after "
                          f"{p1_draws} draw(s)); P0's battlefield untouched")
                else:
                    a5 = ("failed", f"unexpected routing: new_on_p1_bf={new_names} "
                          f"new_on_p0_bf={new_p0_names} ayula_zone={post_zone} "
                          f"p1_lib {p1_lib_pre}->{p1_lib_post} (expected "
                          f"{exp_lib}), draws_sane={draws_sane}")
            ass["A5_owner_routing"] = a5[0]
            notes.append(f"A5 {a5[0]}: {a5[1]}")
            # A6
            stack_empty = not (post_st.get("stack") or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if stack_empty and wf in ("Priority", None):
                ass["A6_cleanup"] = "passed"
                notes.append(f"A6 passed: stack empty, waiting_for={wf}, game proceeding")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"A6 FAILED: stack={[str(e)[:60] for e in (post_st.get('stack') or [])][:3]} "
                             f"waiting_for={wf}")
        else:
            for k in ("A4_zone_correct", "A5_owner_routing", "A6_cleanup"):
                ass[k] = "failed"
            notes.append("A4/A5/A6 failed: pre.json or post.json missing")

        if ass["A1_setup_ok"] == "passed" and any(
                ass[k] == "failed" for k in ("A3_replacement_offered",
                                            "A4_zone_correct",
                                            "A5_owner_routing")):
            verdict = "reproduced"
        elif all(v == "passed" for v in ass.values()):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] != "passed" or ass["A2_cast_targeted"] != "passed":
            verdict = "blocked"
        else:
            verdict = "reproduced"
        notes.append(f"verdict={verdict} cz_choice={CZ_CHOICE}")

        run = {
            "run_id": RUN_ID, "issue": 6912, "cz_choice": CZ_CHOICE,
            "verdict": verdict, "validated_at": "2026-09-12",
            "server": SERVER_IDENTITY,
            "server_run_note": "reused pinned v0.81.3 server on 127.0.0.1:9374",
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6912.py", "rb").read()).hexdigest(),
            "format_config": "CommanderDraft (>=60 cards, no singleton; "
                             "commanders placed in command zone)",
            "decks": {"P0": {"main": P0_MAIN, "commander": P0_COMMANDER},
                      "P1": {"main": P1_DECK, "commander": P1_COMMANDER},
                      "P2": {"main": P2_DECK, "commander": []}},
            "assertions": ass,
            "observations": {k: (sorted(v) if isinstance(v, set) else v)
                             for k, v in obs.items()},
            "driver_state": {k: v for k, v in ST.items()},
            "notes": notes,
            "evidence_files": ["pre.json", "mid.json", "post.json", "run.json",
                               "manifest.sha256", "summary.png",
                               "scenario_6912.py", "wire_log.jsonl",
                               "scenario_run.log"],
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "Dense 12x Chaos Warp / 48x Mountain decks are a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The revealed top card is random; the assertions verify the "
                "routing outcome (owner's library, permanent->BF) rather than a "
                "specific revealed card.",
            ],
            "duration_s": round(dur, 1),
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

    def mulligan_keep(pid, state, key_need, lands_need, max_mulls, land_names):
        hn = hand_lnames(state, pid)
        lands = sum(1 for n in hn if n in land_names)
        mulls = kept.get(f"P{pid}_mulls", 0)
        has_key = key_need is None or key_need in hn
        return (has_key and lands >= lands_need) or mulls >= max_mulls

    async def do_mulligan(c, pid, key_need, lands_need, max_mulls, land_names,
                          tag):
        hn = hand_lnames(c.latest["state"], pid)
        if mulligan_keep(pid, c.latest["state"], key_need, lands_need,
                         max_mulls, land_names):
            kept[f"P{pid}"] = True
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            say(f"{tag} keeps (key_here={key_need in hn if key_need else 'n/a'})")
        else:
            kept[f"P{pid}_mulls"] = kept.get(f"P{pid}_mulls", 0) + 1
            kept.pop(f"P{pid}_bottomed", None)
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Mulligan"}}})
            say(f"{tag} mulligans #{kept[f'P{pid}_mulls']}")

    async def do_bottom(c, pid, tag):
        st = c.latest["state"]
        pending = ((wf_of(st).get("data", {}) or {}).get("pending", []))
        count = 1
        for p in pending:
            if p.get("player") == pid:
                ph = p.get("phase", {}) or {}
                if ph.get("type") == "BottomCards":
                    count = int(ph.get("count", 1))

        def bkey(oid):
            nm = lname(st, oid)
            if nm == WARP and oid not in keep_warp:
                return 0  # bottom spare warps first
            if nm == MOUNTAIN:
                return 1
            return 2  # keep the one warp we need

        hand = [int(o) for o in player_of(st, pid).get("hand", [])]
        warp_oids = [o for o in hand if lname(st, o) == WARP]
        keep_warp = set(warp_oids[:1])
        picks = sorted(hand, key=bkey)[:count]
        kept[f"P{pid}_bottomed"] = True
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} bottoms {count}: {[lname(st, x) for x in picks]}")

    async def answer_replacement(c, pid, tag, acts, st, state):
        """Answer the commander replacement choice for player pid."""
        wf = wf_of(state)
        chooser = wf.get("player", (wf.get("data") or {}).get("player"))
        obs["repl_seen"] = {"turn": state.get("turn_number"),
                            "revision": c.revision, "chooser": chooser}
        obs["repl_chooser"] = chooser
        vi = get_vi(st)
        opps = (vi.get("opportunities", []) or []) if vi else []
        full = None
        for opp in opps:
            resp = opp.get("response", {}) or {}
            if resp.get("type") not in ("exactChoices", "schema"):
                continue
            full = opp
            break
        if full is None:
            cr = find_action(acts, "ChooseReplacement")
            say(f"[{tag}] ReplacementChoice but no vi opportunity; "
                f"ChooseReplacement advertised={cr is not None}")
            wire("repl_no_vi", {"chooser": chooser,
                                "choose_replacement": cr is not None})
            if cr is not None:
                await submit_as_is(c, cr)
                obs["repl_chosen"] = "legacy ChooseReplacement as-is"
                ST["repl_answered"] = True
            return True
        resp = full.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        descs = []
        zone_idx = other_idx = None
        for i, ch in enumerate(chs):
            blob = json.dumps(ch, default=str).lower()
            txt = choice_text(ch)
            descs.append({"i": i, "text": txt[:100],
                          "mentions_command_zone": "command zone" in blob})
            if "command zone" in blob and zone_idx is None:
                zone_idx = i
            elif other_idx is None:
                other_idx = i
        obs["repl_choices"] = descs
        say(f"[{tag}] REPLACEMENT CHOICE chooser={chooser} n={len(chs)} "
            f"descs={json.dumps(descs)}")
        wire("replacement_opportunity",
             {"chooser": chooser,
              "opportunity": json.loads(json.dumps(full, default=str))})
        pick = None
        how = None
        if CZ_CHOICE == "zone" and zone_idx is not None and len(chs) == 2:
            pick, how = zone_idx, "matched 'command zone' text"
        elif CZ_CHOICE == "library" and zone_idx is not None and len(chs) == 2:
            pick, how = other_idx, "non-command-zone choice"
        else:
            pick = 0 if CZ_CHOICE == "zone" else len(chs) - 1
            how = (f"GUESS positional (choice texts ambiguous): "
                   f"idx={pick}")
            obs["repl_guess"] = how
            notes.append("replacement choice texts ambiguous; " + how)
        choice = chs[pick]
        obs["repl_chosen"] = {"i": pick, "text": choice_text(choice)[:100],
                              "how": how}
        await answer_vi(c, full, choice, tag)
        ST["repl_answered"] = True
        ST["repl_answered_at"] = time.time()
        ST["frozen"] = True  # freeze board development: no more land drops /
        # casts; priority passes only, so post.json is a clean
        # post-resolution snapshot
        return True

    async def discard_tick(c, pid, tag, acts, st, state):
        """Answer DiscardToHandSize by discarding a land. Returns True if acted."""
        wtype = wf_of(state).get("type") or ""
        if wtype != "DiscardToHandSize":
            return False
        vi = get_vi(st)
        if not vi:
            return False
        land_nm = MOUNTAIN if pid == 0 else FOREST
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            if not chs:
                continue
            pick = next((ch for ch in chs
                         if land_nm in choice_text(ch).lower()), chs[0])
            say(f"[{tag}] discarding to hand size: {choice_text(pick)[:40]}")
            wire("discard", {"who": tag, "choice": choice_text(pick)[:60]})
            await answer_vi(c, opp, pick, tag)
            prompt_first_seen[iid] = {"t0": time.time(), "done": True}
            return True
        return False

    async def generic_prompt(c, pid, tag, st, state):
        """Log + (after a grace period) affirmatively answer unexpected
        resolution prompts for pid. Returns True if it acted."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in prompt_first_seen and prompt_first_seen[iid].get("done"):
                continue
            blob = json.dumps(opp, default=str)
            low = blob.lower()
            entry = prompt_first_seen.setdefault(
                iid, {"t0": time.time(), "done": False, "blob": blob[:800]})
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            obs["reveal_prompts"].append(
                {"who": tag, "iid": str(iid)[:8],
                 "n_choices": len(chs),
                 "texts": [choice_text(ch)[:60] for ch in chs][:6],
                 "mentions_reveal": "reveal" in low})
            say(f"[{tag}] UNEXPECTED PROMPT iid={iid} n={len(chs)} "
                f"texts={[choice_text(ch)[:40] for ch in chs][:4]}")
            wire("unexpected_prompt",
                 {"who": tag,
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if time.time() - entry["t0"] < 15:
                continue  # grace period: keep observing
            pick = None
            for ch in chs:
                t = choice_text(ch).lower()
                b = json.dumps(ch, default=str).lower()
                if ("reveal" in low and ("yes" in t or '"true"' in b
                                        or "reveal" in t)):
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    t = choice_text(ch).lower()
                    if "yes" in t or "true" in t:
                        pick = ch
                        break
            if pick is None and chs:
                pick = chs[0]
            if pick is not None:
                say(f"[{tag}] auto-answering prompt after 15s stall: "
                    f"{choice_text(pick)[:60]}")
                wire("auto_answer", {"who": tag, "iid": str(iid)[:8],
                                     "choice": choice_text(pick)[:80]})
                obs["auto_answered"].append(
                    {"who": tag, "choice": choice_text(pick)[:80]})
                await answer_vi(c, opp, pick, tag)
                entry["done"] = True
                acted = True
        return acted

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, mid_exported, post_exported
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                await do_mulligan(p0, 0, WARP, 3, 3, (MOUNTAIN,), "P0")
                return
            if find_action(acts, "SelectCards") and last_select.get(0) != p0.revision:
                last_select[0] = p0.revision
                await do_bottom(p0, 0, "P0")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # warp resolution bookkeeping
        if ST["warp_cast"]:
            obs["wf_types_resolution"].add(wtype)
        # mid export at the first tick after the replacement answer
        if ST["repl_answered"] and not mid_exported:
            try:
                mid = await p0.export_state()
                with open(f"{EVDIR}/mid.json", "w") as f:
                    f.write(mid)
                mid_exported = True
                say("exported MID (post replacement answer)")
            except Exception as e:
                notes.append(f"mid export failed: {e}")
        # post export once the atomic resolution has had time to complete;
        # the board is frozen from the replacement answer on, so this is clean
        if (ST["repl_answered"] and not post_exported
                and time.time() - (ST["repl_answered_at"] or 0) > 4):
            say("post-resolution window reached; exporting POST")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
                ST["post_at"] = time.time()
                say("exported POST")
            except Exception as e:
                notes.append(f"post export failed: {e}")
            return
        # target selection for the warp cast
        if (wtype == "TargetSelection" and ST["warp_cast"]
                and not ST["target_answered"]):
            ay = bf_id(state, 1, AYULA)
            vi = get_vi(st)
            if vi and ay is not None:
                for opp in vi.get("opportunities", []) or []:
                    resp = opp.get("response", {}) or {}
                    if resp.get("type") not in ("exactChoices", "schema"):
                        continue
                    data = resp.get("data", {}) or {}
                    chs = data.get("choices") or data.get("candidates") or []
                    pick = None
                    for ch in chs:
                        if ref_oid_of_candidate(ch) == ay:
                            pick = ch
                            break
                    if pick is None:
                        for ch in chs:
                            if AYULA in choice_text(ch).lower():
                                pick = ch
                                break
                    if pick is not None:
                        ST["warp_target_oid"] = ay
                        ST["target_answered"] = True
                        say(f"P0 targets Ayula (oid {ay}) with Chaos Warp")
                        wire("warp_target",
                             {"target_oid": ay,
                              "candidate": json.loads(json.dumps(
                                  {k: pick.get(k) for k in
                                   ("id", "text", "label", "name")},
                                  default=str))})
                        await answer_vi(p0, opp, pick, "P0")
                        return
            say("P0 TargetSelection pending but no Ayula candidate yet")
            wire("target_selection_pending",
                 {"vi": bool(vi), "ayula_bf": ay})
            return
        # replacement choice wrongly offered to P0: record + answer to keep
        # liveness (the wrong-player offer itself is the A3 failure)
        if wtype == "ReplacementChoice" and ST["warp_cast"] \
                and not ST["repl_answered"]:
            wf = wf_of(state)
            chooser = wf.get("player", (wf.get("data") or {}).get("player"))
            if chooser == 0:
                obs["repl_chooser_wrong"] = True
                say("P0 offered the commander replacement choice (WRONG PLAYER)")
                await answer_replacement(p0, 0, "P0", acts, st, state)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p0.send_action({"type": "DeclareAttackers",
                                      "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers",
                                      "data": sub["data"]})
            return
        if await discard_tick(p0, 0, "P0", acts, st, state):
            return
        if not my_priority(state, 0):
            # unexpected P0 prompt during resolution: log, grace, answer
            if ST["warp_cast"] and not ST["repl_answered"]:
                if await generic_prompt(p0, 0, "P0", st, state):
                    return
            return
        # ---- P0 priority ----
        # hold: warp in flight -> only pass (never land-drop or cast)
        if ST["warp_cast"] and not ST["repl_answered"]:
            for a in acts:
                if a["type"] == "PassPriority":
                    await submit_as_is(p0, a)
                    return
            return
        # pre-export + cast warp
        ay = bf_id(state, 1, AYULA)
        if (not ST["warp_cast"]
                and state.get("phase") == "PreCombatMain"
                and state.get("active_player") == 0
                and ay is not None
                and WARP in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, MOUNTAIN)) >= 3):
            if not pre_exported:
                say("PRE: exporting (Ayula on P1 BF, warp in hand, 3+ mountains)")
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_exported = True
            for a in acts:
                if "cast" in a["type"].lower():
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id") or d.get("card_id")
                    if isinstance(oid, int) and lname(state, oid) == WARP:
                        ST["warp_cast"] = True
                        ST["warp_cast_at"] = time.time()
                        obs["cast_turn"] = state.get("turn_number")
                        say(f"P0 casts Chaos Warp via {a['type']}")
                        wire("cast_warp", {"action": a["type"]})
                        await submit_as_is(p0, a)
                        return
        if await discard_tick(p0, 0, "P0", acts, st, state):
            return
        # land drop
        if not ST["frozen"] and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
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
        nonlocal pre_exported
        wtype = wf_of(state).get("type") or ""
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                await do_mulligan(p1, 1, None, 2, 3, (FOREST,), "P1")
                return
            if find_action(acts, "SelectCards") and last_select.get(1) != p1.revision:
                last_select[1] = p1.revision
                await do_bottom(p1, 1, "P1")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        # the commander replacement choice belongs to P1 (the owner)
        if wtype == "ReplacementChoice" and ST["warp_cast"] \
                and not ST["repl_answered"]:
            wf = wf_of(state)
            chooser = wf.get("player", (wf.get("data") or {}).get("player"))
            if chooser == 1:
                await answer_replacement(p1, 1, "P1", acts, st, state)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p1.send_action({"type": "DeclareAttackers",
                                      "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers",
                                      "data": sub["data"]})
            return
        if not my_priority(state, 1):
            if ST["warp_cast"] and not ST["repl_answered"]:
                if await generic_prompt(p1, 1, "P1", st, state):
                    return
            return
        if await discard_tick(p1, 1, "P1", acts, st, state):
            return
        # ---- P1 priority ----
        # cast Ayula from the command zone when affordable (only before the
        # warp test; never recast after the warp was cast)
        if (not ST["warp_cast"]
                and bf_id(state, 1, AYULA) is None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and state.get("active_player") == 1
                and len(untapped_lands(state, 1, FOREST)) >= 2):
            for a in acts:
                if "cast" in a["type"].lower():
                    d = a.get("data", {}) or {}
                    oid = d.get("object_id") or d.get("card_id")
                    if isinstance(oid, int) and lname(state, oid) == AYULA:
                        say(f"P1 casts Ayula (commander) via {a['type']}")
                        wire("cast_ayula", {"action": a["type"]})
                        await submit_as_is(p1, a)
                        return
        if not ST["frozen"] and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
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
            if find_action(acts, "MulliganDecision") and not kept.get("P2"):
                await do_mulligan(p2, 2, None, 2, 2, (FOREST,), "P2")
                return
            if find_action(acts, "SelectCards") and last_select.get(2) != p2.revision:
                last_select[2] = p2.revision
                await do_bottom(p2, 2, "P2")
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p2, a)
                return
        if wtype == "DeclareAttackers" and state.get("active_player") == 2:
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p2.send_action({"type": "DeclareAttackers",
                                      "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p2.send_action({"type": "DeclareBlockers",
                                      "data": sub["data"]})
            return
        if not my_priority(state, 2):
            return
        if await discard_tick(p2, 2, "P2", acts, st, state):
            return
        if not ST["frozen"] and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
                and state.get("active_player") == 2:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p2, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p2, a)
                return

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
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
        # stall watchdog: warp cast but the replacement never got answered
        if (ST["warp_cast"] and not ST["repl_answered"]
                and time.time() - (ST["warp_cast_at"] or 0) > 180
                and not post_exported):
            notes.append("watchdog: 180s since warp cast without the "
                         "replacement being answered; exporting states and "
                         "finishing")
            say("WATCHDOG: replacement never answered; finishing with evidence")
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"watchdog post export failed: {e}")
            await finish()
            return
        if post_exported and time.time() - (ST["post_at"] or 0) > 5:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0hand={hand_lnames(s, 0)} "
                f"P1hand={hand_lnames(s, 1)} ayula={bf_id(s, 1, AYULA)} "
                f"life={[life_of(s, 0), life_of(s, 1)]} "
                f"stack={len(s.get('stack') or [])} pre={pre_exported} "
                f"warp={ST['warp_cast']}/repl={ST['repl_answered']} "
                f"frozen={ST['frozen']} mid={mid_exported} "
                f"post={post_exported}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
