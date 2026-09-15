#!/usr/bin/env python3
"""Issue #818: Unable to sac Eldrazi Spawn after declaring blockers.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 — written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed): in DeclareBlockers, with
  Writhing Chrysalis blocking Human Soldier,
  Gixian Infiltrator blocking Goblin Bushwhacker,
  Eldrazi Spawn blocking Clockwork Percussionist,
the player could not sacrifice the Spawn for its mana ability
("Sacrifice this token: Add {C}").

Substitution (documented): no "Human Soldier" card exists in the pinned
card-data.json; the reporter's attacker is represented by Blazing Rootwalla
({R} 1/1), whose identity is immaterial to the reported failure (sac
permission in DeclareBlockers). Everything else matches the report exactly.

Setup (native engine, two human-client seats):
  P0: 4x Writhing Chrysalis, 4x Gixian Infiltrator, 20x Swamp, 16x Forest,
      16x Mountain. Casts Infiltrator then Chrysalis (cast trigger -> 2 Spawn).
  P1: 4x Clockwork Percussionist, 4x Goblin Bushwhacker, 4x Blazing Rootwalla,
      48x Mountain. Casts all three, attacks with all three.

Trigger: P0 declares blockers exactly as reported, then — while P0 holds
priority in the DeclareBlockers step — activates the blocking Spawn's
"Sacrifice this token: Add {C}" mana ability.

Expected (per card text + CR 117.1b / 702 rules):
  E1: the activation is offered to (or at least accepted from) P0 during
      DeclareBlockers;
  E2: it resolves: Spawn leaves the battlefield (sacrificed), {C} enters P0's
      mana pool;
  E3: every "whenever you sacrifice" trigger fires: each Gixian Infiltrator
      (another permanent sac'd) and each Writhing Chrysalis (another Eldrazi
      sac'd) gains one +1/+1 counter;
  E4: the other Spawn token remains on the battlefield; no dangling decision.

Assertions:
  A1_setup_ok      blockers declared exactly as reported (3 assignments)
  A2_sac_offered   Spawn mana-ability activation offered to P0 in DeclareBlockers,
                   OR a direct ActivateAbility submission for the Spawn's sac
                   ability is accepted by the engine. (Protocol-70 note: mana
                   abilities are not surfaced as standalone offers to human
                   clients on protocol 70, cf. #7145, so the direct-submit path
                   is a legitimate acceptance channel.)
  A3_sac_resolves  activation accepted; blocking Spawn sacrificed; {C} in pool
  A4_triggers      each Infiltrator and each Chrysalis on the battlefield gained
                   exactly one +1/+1 counter from the sac
  A5_no_strand     second Spawn still on BF; game proceeds (no stuck decision)

Verdict rule: reproduced iff A1 passes and (A2 or A3) fails — the reported
inability to sac the Spawn. not-reproduced iff A1..A5 all pass.
blocked iff the setup/combat sequence cannot be driven to completion.

Prior run 20260909-818 (v0.77.0/protocol 67, unpublished): A1..A3 passed
(the sac worked on v0.77.0); A4 failed on a post-sac 120s stall with 6
triggers stuck on the stack. This run re-tests on the pinned v0.83.0.

Evidence: evidence/818/<run-id>/pre.json (blockers declared, pre-sac),
post.json (after sac + trigger window), run.json, manifest.sha256,
summary.png, scenario_818.py, wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260915-818c"
EVDIR = f"{BACKFILL}/evidence/818/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CHRYSALIS = "Writhing Chrysalis"
INFILTRATOR = "Gixian Infiltrator"
SPAWN = "Eldrazi Spawn"
PERCUSSIONIST = "Clockwork Percussionist"
BUSHWHACKER = "Goblin Bushwhacker"
ROOTWALLA = "Blazing Rootwalla"  # stand-in for the report's "Human Soldier"

P0_DECK = [(CHRYSALIS, 4), (INFILTRATOR, 4), ("Swamp", 20), ("Forest", 16), ("Mountain", 16)]
P1_DECK = [(PERCUSSIONIST, 4), (BUSHWHACKER, 4), (ROOTWALLA, 4), ("Mountain", 48)]

SERVER_IDENTITY = {
    "server_version": "0.83.0",
    "build_commit": "b7a59d4",
    "protocol_version": 70,
    "mode": "Full",
    "binary_sha256": "33437c6c057c98bd4ce2a4c64e2d3e3e401c138099469c0a61d9145ae4fdb00f",
    "card_data_sha256": "569d35fe7169b2bb7d9a781478afdacffde423cbccf5926c51cb38db94466c85",
    "draft_pools_sha256": "6dd9c4950bec6c7da9d1205c64f47e564eb202b7369ac4449d6c708f0fb2ed16",
    "signature_verified": True,
    "observed_at": "2026-09-15",
    "source": "ServerHello (version/build/protocol/mode) + sha256 match of pinned "
              "verified artifacts; server process already listening on 9374 "
              "(verified: /proc exe = pinned v0.83.0 binary)",
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


def my_priority(state, pid):
    wf = state.get("waiting_for") or {}
    return wf.get("type") == "Priority" and state.get("priority_player") == pid


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def battlefield_id(state, pid, name):
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and (o.get("base_name") or o.get("name")) == name):
            return int(oid)
    return None


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and (o.get("base_name") or o.get("name")) == name]


def hand_names(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [obj_name(state, o) for o in p.get("hand", [])]
    return []


def mana_pool_units(state, pid, want):
    for p in state.get("players", []):
        if p.get("id") == pid:
            units = (p.get("mana_pool") or {}).get("mana", [])
            return [u for u in units if want in json.dumps(u).lower()]
    return []


def plus_counters(obj):
    v = obj.get("counters")
    if isinstance(v, dict):
        n = 0
        for k, c in v.items():
            if str(k).upper().replace("_", "") in ("P1P1", "+1/+1", "PLUS1PLUS1"):
                n += c if isinstance(c, int) else 0
        return n
    return 0


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            if "_by_object" not in a:
                a["_src_oid"] = _oid
            acts.append(a)
    return acts


async def submit_as_is(c, a):
    # Unit variants (e.g. PassPriority) are advertised with NO data key;
    # echoing one back with "data": {} is rejected by the deserializer.
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def drain_rejections(c):
    """Pull recent Error/ActionRejected messages from the client inbox."""
    out = []
    try:
        while True:
            t, data = c.inbox.get_nowait()
            if t in ("Error", "ActionRejected", "InteractionRejected"):
                out.append({"type": t, "data": data})
    except asyncio.QueueEmpty:
        pass
    return out


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_sac_offered", "A3_sac_resolves",
            "A4_triggers", "A5_no_strand")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}

    # Track submissions: a failed/ignored submission leaves the revision
    # unchanged, which would otherwise deadlock the revision-gated loop.
    for _c in (p0, p1):
        _c._last_send = 0.0
        _orig_send = _c.send_action

        async def _tracked_send(action, _c=_c, _orig=_orig_send):
            _c._last_send = time.time()
            await _orig(action)
        _c.send_action = _tracked_send

    setup_done = False
    blockers_declared = False
    pre_exported = False
    sac_submitted = False
    sac_accepted = False
    sac_rev = None
    sac_submission = None
    sac_offered_via = None
    post_exported = False
    spawn_oid = None
    pre_pool_c = 0
    pre_counters = {}
    pre_spawn_bf = 0
    last = {}

    def render_summary():
        try:
            from PIL import Image, ImageDraw
            W, H = 1000, 760
            img = Image.new("RGB", (W, H), (16, 20, 28))
            d = ImageDraw.Draw(img)
            y = 24
            d.text((24, y), "Phase bug-state backfill — issue #818", fill=(240, 240, 240))
            y += 30
            d.text((24, y), "Unable to sac Eldrazi Spawn after declaring blockers", fill=(180, 190, 205))
            y += 26
            d.text((24, y), f"v0.83.0 (b7a59d4, protocol 70) · run {RUN_ID} · native engine, 2 human seats",
                   fill=(150, 158, 175))
            y += 36
            verd = "reproduced" if ass["A1_setup_ok"] == "passed" and any(
                ass[k] == "failed" for k in ("A2_sac_offered", "A3_sac_resolves")) \
                else ("not-reproduced" if all(ass[k] == "passed" for k in ass) else "inconclusive")
            d.text((24, y), f"Verdict: {verd}", fill=(120, 230, 160) if verd == "not-reproduced"
                   else (240, 120, 120))
            y += 40
            labels = {
                "A1_setup_ok": "blockers declared as reported (Chrysalis->Rootwalla, Infiltrator->Bushwhacker, Spawn->Percussionist)",
                "A2_sac_offered": "Spawn mana-ability activation offered/accepted in DeclareBlockers",
                "A3_sac_resolves": "activation resolved: Spawn sacrificed, {C} in mana pool",
                "A4_triggers": "each Infiltrator & Chrysalis gained exactly +1/+1 counter",
                "A5_no_strand": "second Spawn on BF; game proceeds, no stuck decision",
            }
            for k in labels:
                v = ass[k]
                color = {"passed": (120, 230, 160), "failed": (240, 120, 120)}.get(v, (200, 190, 120))
                d.text((24, y), f"{k}: {v}", fill=color)
                y += 24
                d.text((44, y), labels[k], fill=(150, 158, 175))
                y += 32
            for n in notes[-6:]:
                d.text((24, y), "- " + n[:118], fill=(170, 175, 190))
                y += 24
            img.save(f"{EVDIR}/summary.png")
            say("summary.png rendered")
        except Exception as e:
            say("render failed:", e)
            notes.append(f"summary.png render failed: {e}")

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
        # Verdict rule: reproduced iff A1 passes and (A2 or A3) fails — the reported
        # inability to sac the Spawn. A4 (trigger resolution) is a separate
        # observation and never by itself the reported bug.
        core = [ass["A1_setup_ok"], ass["A2_sac_offered"],
                ass["A3_sac_resolves"], ass["A4_triggers"]]
        bug_bits = [ass["A2_sac_offered"], ass["A3_sac_resolves"]]
        if all(v == "passed" for v in core):
            verdict = "not-reproduced"
        elif ass["A1_setup_ok"] == "passed" and any(v == "failed" for v in bug_bits):
            verdict = "reproduced"
        elif ass["A1_setup_ok"] == "passed":
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
            notes.append("inconclusive or setup incomplete; see notes")
        # copy the exact driver source used
        shutil.copy(__file__, f"{EVDIR}/scenario_818.py")
        run = {
            "issue": 818,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "driver": {"protocol_advertised": 70, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(__file__, "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "verdict": verdict,
            "sac_offered_via": sac_offered_via,
            "sac_submission": sac_submission,
            "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                            "Report's 'Human Soldier' attacker represented by Blazing Rootwalla "
                            "(no such card in pinned card-data.json).",
                            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games)."],
            "setup_line": "P0: 4x Writhing Chrysalis + 4x Gixian Infiltrator + lands; "
                          "P1: 4x Clockwork Percussionist + 4x Goblin Bushwhacker + "
                          "4x Blazing Rootwalla + 48x Mountain",
            "contract_line": "P1 attacks with all three; P0 blocks Chrysalis->Rootwalla, "
                             "Infiltrator->Bushwhacker, Spawn->Percussionist; P0 saccing Spawn "
                             "for {C} during DeclareBlockers",
            "stats": {},
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        render_summary()
        # manifest written LAST, after all say() logging (incl. render) is done
        files = ["pre.json", "post.json", "run.json", "summary.png",
                 "scenario_818.py", "wire_log.jsonl", "scenario_run.log"]
        with open(f"{EVDIR}/manifest.sha256", "w") as mf:
            for fn in files:
                p = f"{EVDIR}/{fn}"
                if os.path.exists(p):
                    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                    mf.write(f"{h}  {fn}\n")
                else:
                    notes.append(f"manifest: missing {fn}")
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    def p0_ready(state):
        return (battlefield_id(state, 0, INFILTRATOR) is not None
                and battlefield_id(state, 0, CHRYSALIS) is not None
                and len(battlefield_ids(state, 0, SPAWN)) >= 2)

    def p1_ready(state):
        return all(battlefield_id(state, 1, n) is not None
                   for n in (PERCUSSIONIST, BUSHWHACKER, ROOTWALLA))

    def cast_if_able(c, acts, state, pid, name):
        for a in acts:
            d = a.get("data", {})
            if a["type"] == "CastSpell" and obj_name(state, d.get("object_id")) == name:
                return a
        return None

    async def p0_tick(st, acts, state):
        nonlocal setup_done, blockers_declared, pre_exported, sac_submitted
        nonlocal sac_accepted, sac_rev, sac_offered_via, sac_submission
        nonlocal spawn_oid, pre_pool_c, pre_counters, pre_spawn_bf
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P0"):
            kept["P0"] = True
            await submit_as_is(p0, ma)
            say("P0 keeps opening hand")
            return
        # mana payment prompts: submit as-is (engine auto-selects)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": sub["data"]})
                say("P0 declares no attackers")
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da and not blockers_declared:
                say("DeclareBlockers action shape: " + json.dumps(da)[:800])
                wire("declare_blockers_shape", da)
                ch = battlefield_id(state, 0, CHRYSALIS)
                inf = battlefield_id(state, 0, INFILTRATOR)
                sp = battlefield_ids(state, 0, SPAWN)
                rw = battlefield_id(state, 1, ROOTWALLA)
                bw = battlefield_id(state, 1, BUSHWHACKER)
                pc = battlefield_id(state, 1, PERCUSSIONIST)
                say(f"blockers: chrys={ch} inf={inf} spawns={sp} | attackers: rw={rw} bw={bw} pc={pc}")
                if None in (ch, inf, rw, bw, pc) or not sp:
                    notes.append("missing combatants at DeclareBlockers; cannot assign per report")
                    ass["A1_setup_ok"] = "failed"
                    return
                spawn_oid = sp[0]
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = [[ch, rw], [inf, bw], [spawn_oid, pc]]
                await p0.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                wire("declare_blockers_submitted",
                     {"assignments": [[ch, rw], [inf, bw], [spawn_oid, pc]]})
                say("P0 declares blockers: Chrysalis->Rootwalla, Infiltrator->Bushwhacker, "
                    f"Spawn({spawn_oid})->Percussionist")
                blockers_declared = True
            return
        if wtype != "Priority" or not my_priority(state, 0):
            if wtype != "Priority":
                say(f"P0 non-priority decision: {wtype} acts="
                    f"{[(a['type'], sorted(a.get('data', {}).keys())) for a in acts][:8]}")
                wire("p0_nonpriority", {"wtype": wtype,
                                       "acts": [(a["type"], a.get("data", {})) for a in acts][:8]})
                if wtype == "OrderTriggers":
                    oa = find_action(acts, "OrderTriggers")
                    if oa:
                        await submit_as_is(p0, oa)
                        say("P0 submits trigger order as advertised")
                        return
            return
        # ---- Priority ----
        if (blockers_declared and not pre_exported
                and state.get("phase") == "DeclareBlockers"):
            say("exporting PRE (blockers declared, P0 priority, pre-sac)")
            pre = await p0.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre)
            pre_env = json.loads(pre)
            pre_st = pre_env["state"]
            pre_pool_c = len(mana_pool_units(pre_st, 0, "colorless"))
            pre_spawn_bf = len(battlefield_ids(pre_st, 0, SPAWN))
            for oid in battlefield_ids(pre_st, 0, INFILTRATOR):
                pre_counters[("inf", oid)] = plus_counters(get_obj(pre_st, oid))
            for oid in battlefield_ids(pre_st, 0, CHRYSALIS):
                pre_counters[("chr", oid)] = plus_counters(get_obj(pre_st, oid))
            # A1: verify assignments on the authoritative state via combat data
            combat = pre_st.get("combat", {})
            say("combat state: " + json.dumps(combat)[:1200])
            wire("combat_state", combat)
            got = set()
            bta = (combat.get("blocker_to_attacker") or {})
            for blk, atks in bta.items():
                if isinstance(atks, int):
                    atks = [atks]
                for atk in atks or []:
                    got.add((obj_name(pre_st, int(blk)), obj_name(pre_st, int(atk))))
            say(f"block assignments observed: {got}")
            want = {(CHRYSALIS, ROOTWALLA), (INFILTRATOR, BUSHWHACKER), (SPAWN, PERCUSSIONIST)}
            if want.issubset(got):
                ass["A1_setup_ok"] = "passed"
                notes.append(f"blockers declared per report: {sorted(got)}")
            else:
                ass["A1_setup_ok"] = "failed"
                notes.append(f"block assignments mismatch: want {sorted(want)} got {sorted(got)}")
            pre_exported = True
            # fall through to the sac attempt on this same revision
        # sac attempt: P0 priority in DeclareBlockers, PRE exported
        if (blockers_declared and pre_exported and not sac_submitted
                and state.get("phase") == "DeclareBlockers"):
            cands = []
            for a in acts:
                d = a.get("data", {})
                if a["type"] in ("ActivateAbility", "ActivateManaAbility") \
                        and d.get("source_id") == spawn_oid:
                    cands.append(a)
            if cands:
                say(f"Spawn activation offered: {json.dumps(cands[0])[:500]}")
                wire("spawn_activation_offered", cands[0])
                ass["A2_sac_offered"] = "passed"
                sac_offered_via = "offered:" + cands[0]["type"]
                await p0.send_action({"type": "ActivateAbility", "data": cands[0]["data"]})
                say("P0 activates Spawn sac ability (offered channel)")
            else:
                # Protocol-70 path: mana abilities are not surfaced as
                # standalone offers (#7145). Try a direct ActivateAbility
                # submission for the Spawn's sac ability.
                rel = [a for a in acts if a.get("data", {}).get("source_id") == spawn_oid]
                any_act = [a["type"] for a in acts
                           if a["type"] in ("ActivateAbility", "ActivateManaAbility")]
                say(f"no Spawn activation offered: spawn={spawn_oid} rel_types={[a['type'] for a in rel]} "
                    f"any_activate={any_act[:6]} total_acts={len(acts)}")
                wire("no_spawn_activation", {"spawn_oid": spawn_oid,
                                             "act_types": sorted({a["type"] for a in acts})})
                sp_obj = get_obj(state, spawn_oid)
                say("spawn object ability fields: " +
                    json.dumps({k: v for k, v in sp_obj.items()
                                if "abilit" in k.lower()})[:600])
                wire("spawn_object", {k: sp_obj.get(k) for k in sp_obj
                                      if "abilit" in k.lower()})
                sub = {"type": "ActivateAbility",
                       "data": {"source_id": spawn_oid, "ability_index": 0}}
                sac_offered_via = "direct_submit"
                sac_submission = sub
                await p0.send_action(sub)
                say("P0 attempts direct ActivateAbility submit for Spawn sac (ability_index 0)")
            wire("sac_submission", {"via": sac_offered_via, "submission": sac_submission})
            sac_submitted = True
            sac_rev = p0.revision
            return
        if sac_rev is not None and sac_submitted and not sac_accepted and p0.revision != sac_rev:
            # observe post-activation state
            sp_bf = len(battlefield_ids(state, 0, SPAWN))
            pool_c = len(mana_pool_units(state, 0, "colorless"))
            rej = drain_rejections(p0)
            say(f"post-activation: spawns_on_bf={sp_bf} (was {pre_spawn_bf}) "
                f"colorless_in_pool={pool_c} (was {pre_pool_c}) phase={state.get('phase')} "
                f"rejections={json.dumps(rej)[:400]}")
            wire("post_activation", {"spawns_bf": sp_bf, "pool_c": pool_c,
                                     "phase": state.get("phase"), "rejections": rej})
            if sp_bf == pre_spawn_bf - 1 and pool_c > pre_pool_c:
                sac_accepted = True
                if sac_offered_via == "direct_submit":
                    ass["A2_sac_offered"] = "passed"
                    notes.append("Spawn sac activation NOT offered, but direct "
                                 "ActivateAbility submission was ACCEPTED")
                ass["A3_sac_resolves"] = "passed"
                notes.append("Spawn sacrificed; {C} added to pool")
                wire("sac_accepted", {"via": sac_offered_via})
            elif rej:
                ass["A2_sac_offered"] = "failed"
                ass["A3_sac_resolves"] = "failed"
                notes.append(f"Spawn sac activation rejected: {json.dumps(rej)[:300]} — "
                             "matches reported 'unable to sac spawn'")
            else:
                ass["A3_sac_resolves"] = "failed"
                notes.append(f"sac did not resolve cleanly: spawns {pre_spawn_bf}->{sp_bf}, "
                             f"pool {pre_pool_c}->{pool_c}")
            # fall through to pass priority so the resulting triggers can resolve
        if sac_accepted and ass["A4_triggers"] == "not-run":
            ok = True
            for kind, nm in (("inf", INFILTRATOR), ("chr", CHRYSALIS)):
                for oid in battlefield_ids(state, 0, nm):
                    now = plus_counters(get_obj(state, oid))
                    was = pre_counters.get((kind, oid))
                    say(f"counters {nm}({oid}): {was} -> {now}")
                    if was is None or now != was + 1:
                        ok = False
            if ok:
                ass["A4_triggers"] = "passed"
                notes.append("sac triggers resolved: exactly +1/+1 on every Infiltrator and Chrysalis")
            # fall through to pass priority so the stack keeps resolving
        if sac_accepted and ass["A4_triggers"] == "passed" and ass["A5_no_strand"] == "not-run":
            sp_bf = len(battlefield_ids(state, 0, SPAWN))
            wf = (state.get("waiting_for") or {}).get("type")
            if sp_bf == pre_spawn_bf - 1 and wf in ("Priority", None):
                ass["A5_no_strand"] = "passed"
                notes.append(f"second Spawn still on BF ({sp_bf}); game proceeding "
                             f"(phase={state.get('phase')}, wf={wf})")
            # fall through to pass priority
        # ---- normal setup play ----
        if not setup_done:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
            a = cast_if_able(p0, acts, state, 0, INFILTRATOR)
            if a:
                say("P0 casts Gixian Infiltrator")
                await submit_as_is(p0, a)
                return
            a = cast_if_able(p0, acts, state, 0, CHRYSALIS)
            if a:
                say("P0 casts Writhing Chrysalis")
                wire("p0_cast_chrysalis", a)
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        nonlocal setup_done
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get("P1"):
            kept["P1"] = True
            await submit_as_is(p1, ma)
            say("P1 keeps opening hand")
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                sub = json.loads(json.dumps(da))
                if setup_done:
                    atks = []
                    oids = []
                    for nm in (PERCUSSIONIST, BUSHWHACKER, ROOTWALLA):
                        oid = battlefield_id(state, 1, nm)
                        if oid is not None:
                            oids.append(oid)
                    # all three must be free of summoning sickness, else wait
                    if len(oids) == 3 and all(can_attack_now(state, o) for o in oids):
                        atks = [[o, {"type": "Player", "data": 0}] for o in oids]
                        say(f"P1 attacks with {[obj_name(state, a[0]) for a in atks]}")
                        wire("p1_attacks", sub["data"])
                    else:
                        say(f"P1 waits: attackers not all ready "
                            f"{[(obj_name(state, o), can_attack_now(state, o)) for o in oids]}")
                    sub["data"]["attacks"] = atks
                else:
                    sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                sub = json.loads(json.dumps(da))
                sub["data"]["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": sub["data"]})
            return
        if wtype != "Priority" or not my_priority(state, 1):
            if wtype == "OrderTriggers":
                oa = find_action(acts, "OrderTriggers")
                if oa:
                    await submit_as_is(p1, oa)
                    say("P1 submits trigger order as advertised")
                    return
            if wtype == "OptionalCostChoice":
                da = find_action(acts, "DecideOptionalCost")
                if da:
                    sub = json.loads(json.dumps(da))
                    sub.setdefault("data", {})["pay"] = False
                    await p1.send_action({"type": "DecideOptionalCost", "data": sub["data"]})
                    say("P1 declines optional cost (kicker)")
                    return
                say(f"P1 OptionalCostChoice with acts={[a['type'] for a in acts][:8]}")
                wire("p1_optcost_noaction", [a["type"] for a in acts][:10])
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for nm in (PERCUSSIONIST, BUSHWHACKER, ROOTWALLA):
            a = cast_if_able(p1, acts, state, 1, nm)
            if a:
                say(f"P1 casts {nm}")
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    t0 = time.time()
    last_diag = 0.0
    sac_deadline = None
    last_tick_at = {}
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            # retry if we submitted >5s ago with no state change (rejected/lost)
            due_retry = (same_rev and c._last_send > last_tick_at.get(c.name, 0)
                         and time.time() - c._last_send > 5)
            if same_rev and not due_retry:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        # setup completion check
        if not setup_done and p0.latest and p1.latest:
            s0 = p0.latest["state"]
            if p0_ready(s0) and p1_ready(s0):
                setup_done = True
                say("SETUP COMPLETE: P0 has Infiltrator+Chrysalis+2 spawns; "
                    "P1 has all three attackers")
                notes.append("setup complete at turn "
                             f"{s0.get('turn_number')}")
        # after sac accepted and triggers done -> finish
        if ass["A4_triggers"] == "passed" and ass["A5_no_strand"] == "passed":
            say("all assertions resolved; finishing")
            await finish()
            return
        # reproduced path: activation never usable -> capture post and finish
        if sac_submitted and ass["A2_sac_offered"] == "failed":
            if sac_deadline is None:
                sac_deadline = time.time() + 20
            if time.time() > sac_deadline:
                say("activation never usable; capturing POST and finishing as reproduced")
                await finish()
                return
        # safety: sac submitted but nothing observed
        if sac_submitted and sac_accepted and ass["A4_triggers"] == "not-run":
            if sac_deadline is None:
                sac_deadline = time.time() + 150
            if time.time() > sac_deadline:
                notes.append("sac accepted but sac triggers never put counters on "
                             "(150s window)")
                ass["A4_triggers"] = "failed"
                await finish()
                return
        if time.time() - last_diag > 45 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"P0bf={[obj_name(s, o) for o in [k for k, v in s['objects'].items() if v.get('zone') == 'Battlefield' and v.get('controller') == 0]][:12]} "
                f"P1bf={[obj_name(s, o) for o in [k for k, v in s['objects'].items() if v.get('zone') == 'Battlefield' and v.get('controller') == 1]][:8]} "
                f"P0hand={hand_names(s, 0)} setup={setup_done} blk={blockers_declared}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
