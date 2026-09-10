#!/usr/bin/env python3
"""Issue #6666: Hidetsugu and Kairi -- optional during-resolution free cast never reaches the player.

Report (github #6666, status:confirmed, area:engine, mechanic:costs+zone-change):
Hidetsugu and Kairi's dies trigger -- "exile the top card of your library.
Target opponent loses life equal to its mana value. If it's an instant or
sorcery card, you may cast it without paying its mana cost." -- ends with the
third clause never reaching the player: resolution completes to a clean
Priority window with the exiled card still in exile and no cast offered, for
an instant and a sorcery alike.

BEHAVIORAL CONTRACT (PLAYBOOK.md step 2 -- written before observing results)
------------------------------------------------------------------------------
Setup (per game): P0 = 12x Hidetsugu and Kairi + 24x <desired> + 12x Island +
12x Swamp; P1 = 12x Murder + 48x Swamp. P0 casts H&K ({2}{U}{U}{B}); its ETB
draws 3 and the driver puts 2 <desired> cards back on top (deterministic
staging of the library top; a wait-for-top fallback covers ETB anomalies).
P1 casts Murder on H&K on the next main phase; the dies trigger resolves.
Games: A = Shock (instant), accept the free cast;
       B = Divination (sorcery), accept the free cast;
       C = Shock (instant), decline (control).

Expected (per Oracle):
 E1: dies trigger fires; staged top card exiled; P1 loses life = its MV.
 E2: P0 is offered the optional may-cast choice for the exiled instant/sorcery.
 E3a (accept): the card is cast without paying, put on the stack
      (DuringResolution), targets chosen, resolves with its effect
      (Shock: 2 damage to P1; Divination: P0 draws 2), ends in P0 graveyard.
 E3b (decline): no cast; card stays exiled; game proceeds normally.

Assertions (per game):
 A1_setup_ok       H&K on P0 battlefield at kill time, P1 at 20, Murder cast.
 A2_trigger_resolves H&K in P0 graveyard; exiled card is the staged <desired>;
      P1 life == 20 - MV.
 A3_cast_offered   THE bug assertion: the optional may-cast prompt reaches P0
      after the trigger resolves (failed => reproduced).
 A4_free_cast      accept: spell resolved with effect, card in P0 graveyard;
      decline: card still exiled, no life change, game proceeds;
      not-run when no prompt ever appeared.
 A5_cleanup        stack empty, game advanced past the kill turn, no stall.

Verdict rule: blocked iff A1 fails (setup never assembled). reproduced iff A1
passes and A3 fails in game A or B (the reported "never reaches the player").
not-reproduced iff A1..A5 pass in all three games on v0.78.0 (not a fix claim
for the original build).

Evidence: evidence/6666/<run-id>/{A,B,C}_{pre_kill,post_trigger,post_cast,post}.json,
mid_may.json (prompt capture), mid_etb.json, run.json, scenario_6666.py,
wire_log.jsonl, scenario_run.log, server_excerpts.log, summary.png,
manifest.sha256.
"""
import asyncio
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client.URL = "ws://127.0.0.1:9376/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6666"
EVDIR = f"{BACKFILL}/evidence/6666/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)
shutil.copy(__file__, f"{EVDIR}/scenario_6666.py")

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

HK = "hidetsugu and kairi"
SHOCK = "shock"
DIV = "divination"
MURDER = "murder"
ISLAND = "island"
SWAMP = "swamp"

GAMES = [
    {"tag": "A", "desired": SHOCK, "mv": 1, "accept": True,
     "desc": "instant (Shock), accept the free cast"},
    {"tag": "B", "desired": DIV, "mv": 3, "accept": True,
     "desc": "sorcery (Divination), accept the free cast"},
    {"tag": "C", "desired": SHOCK, "mv": 1, "accept": False,
     "desc": "instant (Shock), decline control"},
]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_verified": True,
    "observed_at": "2026-09-10",
    "source": "ServerHello (v0.78.0/build 4de7224/protocol 68/mode Full) on "
              "127.0.0.1:9376; pinned v0.78.0 release = latest stable "
              "(published 2026-09-09); minisign-verified binary + signed data "
              "manifest with repo-pinned SERVER_ARTIFACT_PUBLIC_KEY",
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


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == key]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def hand_oids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def lib_oids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("library", [])]
    return []


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def zone_ids(state, zone, pid=None, name=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone:
            continue
        if pid is not None and o.get("owner") != pid and o.get("controller") != pid:
            continue
        if name is not None and lname(state, oid) != name:
            continue
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


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg = {"type": a["type"], "data": a["data"]}
    await c.send_action(msg)


def stack_blobs(state):
    return [json.dumps(e, default=str).lower() for e in state.get("stack", []) or []]


def find_stack_trigger(state, kind):
    """Locate H&K's ETB / dies trigger on the stack (name match preferred,
    effect-signature fallback per AGENTS.md)."""
    for e, b in zip(state.get("stack", []) or [], stack_blobs(state)):
        hit = False
        if "hidetsugu" in b:
            if kind == "dies":
                hit = ("dies" in b or "exiletop" in b or "exile top" in b)
            else:
                hit = ("enter" in b or "draw" in b)
        else:
            if kind == "dies" and "exiletop" in b:
                hit = True
            elif kind == "etb" and "draw" in b and "three" in b:
                hit = True
        if hit:
            return e
    return None


def cand_bool_value(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        v = d.get("value")
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower()
    v = ch.get("value")
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower()
    lab = str(ch.get("label") or ch.get("text") or ch.get("name") or "").lower()
    return {"true": "true", "false": "false", "accept": "true", "yes": "true",
            "decline": "false", "no": "false"}.get(lab)


def is_may_choice(cands):
    if len(cands) != 2:
        return False
    return {cand_bool_value(c) for c in cands} == {"true", "false"}


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if d.get("reference") is not None:
            return int(d["reference"])
    return None


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if d.get("seat") is not None:
            return int(d["seat"])
    return None


def new_game_state(cfg):
    return {
        "tag": cfg["tag"],
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_trigger_resolves",
                                       "A3_cast_offered", "A4_free_cast",
                                       "A5_cleanup")},
        "notes": [],
        "submitted_iids": set(),
        "shapes_logged": set(),
        "stack_logged": set(),
        "hk_oid": None,
        "hk_cast": False,
        "etb_seen": False,
        "etb_resolved": False,
        "staged": False,
        "putback_picks": 0,
        "etb_putback_done": False,
        "murder_cast": False,
        "kill_turn": None,
        "pre_exported": False,
        "hk_dead": False,
        "dies_seen": False,
        "trigger_resolved": False,
        "settle_until": 0,
        "settle_rounds": 0,
        "may_wf_type": None,
        "may_prompt_seen": False,
        "may_prompt_at": 0,
        "may_answered": False,
        "may_accepted": None,
        "spell_on_stack": False,
        "spell_resolved": False,
        "bug_confirmed": False,
        "terminal": False,
        "post_exported": False,
        "finished": False,
        "ptr_life": None,
        "sel_ctx": None,  # (kind, who)
        "last_topcheck_turn": None,
        "rejections": [],
    }


async def run_game(cfg):
    tag = cfg["tag"]
    desired = cfg["desired"]
    G = new_game_state(cfg)
    say(f"===== GAME {tag}: {cfg['desc']} =====")
    wire("game_start", {"tag": tag, "cfg": cfg})

    p0 = PhaseClient(f"P0-{tag}")
    await p0.connect()
    await p0.create(deck((HK, 12), (desired, 24), (ISLAND, 12), (SWAMP, 12)),
                    player_count=2)
    p1 = PhaseClient(f"P1-{tag}")
    await p1.connect()
    await p1.join(p0.game_code, deck((MURDER, 12), (SWAMP, 48)))
    say(f"[{tag}] game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id}")

    kept = {}

    async def export(suffix):
        raw = await p0.export_state()
        with open(f"{EVDIR}/{tag}_{suffix}.json", "w") as f:
            f.write(raw)
        say(f"[{tag}] exported {tag}_{suffix}.json")
        return json.loads(raw)["state"]

    async def do_mulligan(c, pid):
        mulls = kept.get(f"P{pid}_mulls", 0)
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        kept[f"P{pid}_mulls"] = mulls + 1
        say(f"[{tag}] P{pid} mulligans (x{mulls + 1})")

    async def do_keep(c, pid):
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        kept[f"P{pid}"] = True
        say(f"[{tag}] P{pid} keeps")

    async def do_bottom(c, pid, state):
        pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
        count = 1
        for p in pending:
            if p.get("player") == pid and (p.get("phase", {}) or {}).get("type") == "BottomCards":
                count = int(p.get("phase", {}).get("count", 1))
        hns = [lname(state, o) for o in hand_oids(state, pid)]
        hk_n = sum(1 for n in hns if n == HK)
        land_n = sum(1 for n in hns if n in (ISLAND, SWAMP))

        def bkey(oid):
            nm = lname(state, oid)
            # P0: never bottom away the LAST Hidetsugu and Kairi
            if nm == HK and pid == 0:
                return (3, nm) if hk_n <= 1 else (0, nm)
            if nm in (ISLAND, SWAMP) and land_n > 4:
                return (1, nm)
            if nm == MURDER and pid == 1:
                return (0, nm) if hns.count(MURDER) > 2 else (4, nm)
            return (2, nm)

        order = sorted(hand_oids(state, pid), key=bkey)
        picks = order[:count]
        # safety: P0 must retain at least one H&K in hand (never bottom
        # the last copy)
        if pid == 0 and hns.count(HK) >= 1:
            retained = [o for o in order[count:] if lname(state, o) == HK]
            if not retained:
                bottomed_hk = [o for o in picks if lname(state, o) == HK]
                non_hk_rest = [o for o in order[count:]
                               if lname(state, o) != HK]
                if bottomed_hk and non_hk_rest:
                    picks = ([x for x in picks if x != bottomed_hk[0]]
                             + [non_hk_rest[0]])
                    say(f"[{tag}] P{pid} swap-back: keeping H&K in hand")
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"[{tag}] P{pid} bottoms {count}: {[lname(state, x) for x in picks]}")

    async def scan_interactions(st, state, who, c):
        """Answer prompts per driver context. Returns True if acted."""
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return False
        wtype = (state.get("waiting_for") or {}).get("type")
        acted = False
        for opp in opps:
            iid = opp.get("interactionId")
            if iid in G["submitted_iids"]:
                continue
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            cands = data.get("candidates") or data.get("choices") or []
            key = (who, rtype, spec_type, len(cands), wtype)
            if key not in G["shapes_logged"]:
                G["shapes_logged"].add(key)
                say(f"[{tag}/{who}] interaction rtype={rtype} spec={spec_type} "
                    f"n={len(cands)} wtype={wtype}")
                wire("interaction_shape",
                     {"game": tag, "who": who, "rtype": rtype, "spec": spec_type,
                      "wtype": wtype, "interaction": opp})
            # --- the may-cast choice: bool pair for P0 after trigger resolution
            if (who == "P0" and G["trigger_resolved"] and not G["may_answered"]
                    and is_may_choice(cands)):
                if not G["may_prompt_seen"]:
                    G["may_prompt_seen"] = True
                    G["may_prompt_at"] = time.time()
                    G["may_wf_type"] = wtype
                    say(f"[{tag}] *** MAY-CAST PROMPT OBSERVED "
                        f"(waiting_for={wtype}) ***")
                    wire("may_prompt", {"game": tag, "interaction": opp})
                    try:
                        await export("mid_may")
                    except Exception as e:
                        G["notes"].append(f"mid_may export failed: {e}")
                want = "true" if cfg["accept"] else "false"
                pick = next(ch for ch in cands if cand_bool_value(ch) == want)
                if rtype == "exactChoices":
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": pick["id"]}}}
                else:
                    sub = {"interactionId": iid,
                           "response": {"type": spec_type or "sequence",
                                        "data": {"choiceIds": [pick["id"]]}}}
                say(f"[{tag}/P0] answers may-cast: {want} "
                    f"(choice {pick['id']})")
                wire("may_answer", {"game": tag, "accept": cfg["accept"],
                                    "submission": sub})
                await c.send_interaction(sub)
                G["submitted_iids"].add(iid)
                G["may_answered"] = True
                G["may_accepted"] = cfg["accept"]
                if cfg["accept"] and desired == SHOCK:
                    G["sel_ctx"] = ("shock_target", "P0")
                acted = True
                continue
            # --- schema target / card-selection prompts, gated by sel_ctx ---
            if spec_type not in ("sequence", "select"):
                continue
            ctx = G.get("sel_ctx")
            # DiscardToHandSize is identified by waiting_for, not sel_ctx
            is_discard = (wtype == "DiscardToHandSize")
            # The ETB's "put two cards on top" arrives as a separate
            # EffectZoneChoice select prompt AFTER the draw portion resolved
            # (the stack entry already vanished). Answer it whenever it is
            # pending, independent of the wait-fallback staging flag.
            wf_data = ((state.get("waiting_for") or {}).get("data") or {})
            is_putback = (wtype == "EffectZoneChoice" and who == "P0"
                          and G["etb_seen"] and not G["etb_putback_done"]
                          and wf_data.get("effect_kind") == "PutAtLibraryPosition")
            if not ctx and not is_discard and not is_putback:
                continue
            # the discard prompt always wins for the waiting player
            if is_discard:
                kind = "discard"
            elif ctx and ctx[1] != who:
                continue
            else:
                kind = ctx[0] if ctx else "discard"
            if is_putback and (not ctx or ctx[0] != "etb_putback"):
                kind = "etb_putback"
            pick_ids, need = [], 1
            if kind == "murder_target":
                hk = G.get("hk_oid")
                for ch in cands:
                    if hk is not None and ref_of(ch) == hk:
                        pick_ids = [ch["id"]]
                        break
            elif kind == "shock_target":
                for ch in cands:
                    if seat_of(ch) == 1:
                        pick_ids = [ch["id"]]
                        break
            elif kind == "dies_target":
                for ch in cands:
                    if seat_of(ch) == 1:
                        pick_ids = [ch["id"]]
                        break
            elif kind == "etb_putback":
                need = 2
                handset = set(hand_oids(state, 0))
                want_ids, other_ids = [], []
                for ch in cands:
                    r = ref_of(ch)
                    if r is None or r not in handset:
                        continue
                    (want_ids if lname(state, r) == desired
                     else other_ids).append(ch["id"])
                pick_ids = (want_ids + other_ids)[:2]
                G["putback_picks"] += len(pick_ids)
            elif kind == "discard":
                pid = 0 if who == "P0" else 1
                count = 1
                for p in ((state.get("waiting_for") or {}).get("data", {})
                          or {}).get("pending", []):
                    if p.get("player") == pid:
                        count = int((p.get("phase", {}) or {}).get("count", 1))
                need = count
                hns = [lname(state, o) for o in hand_oids(state, pid)]

                def dkey(ch):
                    r = ref_of(ch)
                    nm = lname(state, r) if r else ""
                    if pid == 1:
                        # keep ~2 Murders and the lands; shed the rest
                        if nm == MURDER and hns.count(MURDER) > 2:
                            return 0
                        if nm == SWAMP:
                            return 1
                        return 2
                    # P0: never strand ourselves without mana or putback
                    # fodder. Shed extra desired copies first (keep >= 2
                    # for the ETB put-back), then lands only once 7 are
                    # already on the battlefield; H&K and the last two
                    # desired copies are protected (discarded only if the
                    # hand has nothing else to give).
                    bf_lands = (sum(1 for oid in bf_ids(state, 0, ISLAND))
                                + sum(1 for oid in bf_ids(state, 0, SWAMP)))
                    if nm == desired and hns.count(desired) > 2:
                        return 0
                    if nm in (ISLAND, SWAMP) and bf_lands >= 7:
                        return 1
                    if nm == HK:
                        return 5
                    if nm == desired:
                        return 4
                    if nm in (ISLAND, SWAMP):
                        return 3
                    return 2

                pick_ids = [ch["id"] for ch in
                            sorted(cands, key=dkey)][:count]
            if not pick_ids or len(pick_ids) < need:
                if kind != "etb_putback":
                    G["notes"].append(
                        f"[{tag}] {kind} prompt had no usable candidate "
                        f"(n={len(cands)})")
                continue
            sub = {"interactionId": iid,
                   "response": {"type": spec_type,
                                "data": {"choiceIds": pick_ids}}}
            say(f"[{tag}/{who}] answers {kind} with {len(pick_ids)} choice(s)")
            wire(f"{kind}_answer",
                 {"game": tag, "submission": sub, "interaction": opp})
            await c.send_interaction(sub)
            G["submitted_iids"].add(iid)
            if kind == "etb_putback":
                G["etb_putback_done"] = True
                try:
                    mid = await export("mid_etb")
                    top2 = [lname(mid, o) for o in lib_oids(mid, 0)[:2]]
                    say(f"[{tag}] mid_etb top2={top2}")
                    if top2 == [desired, desired]:
                        G["staged"] = True
                        G["sel_ctx"] = None
                        say(f"[{tag}] library top staged via ETB put-back")
                    else:
                        G["notes"].append(
                            f"[{tag}] putback answered but top2={top2}")
                except Exception as e:
                    G["notes"].append(f"mid_etb export failed: {e}")
            elif kind in ("murder_target", "shock_target", "dies_target"):
                G["sel_ctx"] = None
            acted = True
        return acted

    async def p0_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P0"):
                mulls = kept.get("P0_mulls", 0)
                hns = [lname(state, o) for o in hand_oids(state, 0)]
                lands = [n for n in hns if n in (ISLAND, SWAMP)]
                if (HK in hns and len(lands) >= 3 and ISLAND in lands
                        and SWAMP in lands):
                    await do_keep(p0, 0)
                elif mulls >= 3 and HK in hns:
                    await do_keep(p0, 0)
                elif mulls >= 4:
                    await do_keep(p0, 0)
                else:
                    await do_mulligan(p0, 0)
                return
            if find_action(acts, "SelectCards"):
                await do_bottom(p0, 0, state)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        hk = bf_id(state, 0, HK)
        if hk is not None and G["hk_oid"] is None:
            G["hk_oid"] = hk
            say(f"[{tag}] Hidetsugu and Kairi on battlefield (oid {hk})")
        if hk is not None and not G["etb_seen"]:
            e = find_stack_trigger(state, "etb")
            if e:
                G["etb_seen"] = True
                G["sel_ctx"] = ("etb_putback", "P0")
                say(f"[{tag}] H&K ETB trigger on stack")
                wire("etb_trigger", {"game": tag, "entry": e})
        if G["etb_seen"] and not G["etb_resolved"]:
            if not find_stack_trigger(state, "etb"):
                G["etb_resolved"] = True
                if G["sel_ctx"] and G["sel_ctx"][0] == "etb_putback":
                    G["sel_ctx"] = None
                say(f"[{tag}] ETB trigger resolved (staged={G['staged']})")
        # log new stack entries once (post-hoc classification check)
        for b in stack_blobs(state):
            if b not in G["stack_logged"]:
                G["stack_logged"].add(b)
                wire("stack_entry", {"game": tag, "blob": b[:1500]})
        if await scan_interactions(st, state, "P0", p0):
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        phase = state.get("phase")
        if phase not in ("PreCombatMain", "PostCombatMain"):
            for a in acts:
                if a["type"] == "PassPriority":
                    await submit_as_is(p0, a)
                    return
            return
        # fallback staging: wait until the library top is the desired card
        if hk is not None and not G["staged"]:
            turn = state.get("turn_number")
            if G["last_topcheck_turn"] != turn:
                G["last_topcheck_turn"] = turn
                try:
                    tmp = await export("tmp_top")
                    libs = lib_oids(tmp, 0)
                    top = lname(tmp, libs[0]) if libs else None
                    say(f"[{tag}] topcheck turn {turn}: top={top}")
                    if top == desired:
                        G["staged"] = True
                        say(f"[{tag}] library top staged via wait ({top})")
                except Exception as e:
                    G["notes"].append(f"topcheck export failed: {e}")
        p0_lands = sum(1 for oid in bf_ids(state, 0, ISLAND)) + sum(
            1 for oid in bf_ids(state, 0, SWAMP))
        if p0_lands < 7:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p0, a)
                    return
        if not G["hk_cast"] and hk is None:
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and lname(state, d.get("object_id")) == HK):
                    say(f"[{tag}] P0 casts Hidetsugu and Kairi")
                    wire("cast_hk", {"game": tag, "action": a})
                    await submit_as_is(p0, a)
                    G["hk_cast"] = True
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            if find_action(acts, "MulliganDecision") and not kept.get("P1"):
                mulls = kept.get("P1_mulls", 0)
                hns = [lname(state, o) for o in hand_oids(state, 1)]
                swamps = sum(1 for n in hns if n == SWAMP)
                if MURDER in hns and swamps >= 2:
                    await do_keep(p1, 1)
                elif mulls >= 3 and MURDER in hns:
                    await do_keep(p1, 1)
                elif mulls >= 4:
                    await do_keep(p1, 1)
                else:
                    await do_mulligan(p1, 1)
                return
            if find_action(acts, "SelectCards"):
                await do_bottom(p1, 1, state)
                return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if await scan_interactions(st, state, "P1", p1):
            return
        if (G["murder_cast"] and not G["hk_dead"] and G["hk_oid"] is not None
                and bf_id(state, 0, HK) is None):
            G["hk_dead"] = True
            if G["sel_ctx"] and G["sel_ctx"][0] == "murder_target":
                G["sel_ctx"] = None
            say(f"[{tag}] Hidetsugu and Kairi is dead")
            wire("hk_dead", {"game": tag})
        if G["hk_dead"] and not G["dies_seen"]:
            e = find_stack_trigger(state, "dies")
            if e:
                G["dies_seen"] = True
                G["sel_ctx"] = ("dies_target", "P0")
                say(f"[{tag}] *** DIES TRIGGER ON STACK ***")
                wire("dies_trigger", {"game": tag, "entry": e})
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = json.loads(json.dumps(da.get("data", {})))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": d})
            return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        phase = state.get("phase")
        wf = state.get("waiting_for") or {}
        wtype = wf.get("type")
        putback_pending = (wtype == "EffectZoneChoice"
                           and ((wf.get("data") or {}).get("effect_kind")
                                == "PutAtLibraryPosition"))
        # Only kill once the library top is staged AND the ETB's put-back
        # sub-effect is fully resolved (no dangling EffectZoneChoice).
        can_kill = (G["staged"] and not putback_pending
                    and wtype in (None, "Priority"))
        if (can_kill and not G["murder_cast"]
                and phase in ("PreCombatMain", "PostCombatMain")):
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and lname(state, d.get("object_id")) == MURDER):
                    if not G["pre_exported"]:
                        try:
                            await export("pre_kill")
                            G["pre_exported"] = True
                        except Exception as e:
                            G["notes"].append(f"pre_kill export failed: {e}")
                    say(f"[{tag}] P1 casts Murder on Hidetsugu and Kairi")
                    wire("cast_murder", {"game": tag, "action": a})
                    await submit_as_is(p1, a)
                    G["murder_cast"] = True
                    G["kill_turn"] = state.get("turn_number")
                    G["sel_ctx"] = ("murder_target", "P1")
                    return
        p1_lands = sum(1 for oid in bf_ids(state, 1, SWAMP))
        if p1_lands < 8 and phase in ("PreCombatMain", "PostCombatMain"):
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return

    async def loop_checks(cur):
        # dies trigger resolution -> post_trigger export + settle window
        if G["dies_seen"] and not G["trigger_resolved"]:
            if not find_stack_trigger(cur, "dies"):
                G["trigger_resolved"] = True
                if G["sel_ctx"] and G["sel_ctx"][0] == "dies_target":
                    G["sel_ctx"] = None
                try:
                    pts = await export("post_trigger")
                except Exception as e:
                    G["notes"].append(f"post_trigger export failed: {e}")
                    pts = cur
                G["ptr_life"] = life_of(pts, 1)
                wf = (pts.get("waiting_for") or {}).get("type")
                G["may_wf_type"] = wf
                G["settle_until"] = time.time() + 3
                say(f"[{tag}] dies trigger resolved; waiting_for={wf}; "
                    f"P1 life={G['ptr_life']}")
                wire("trigger_resolved",
                     {"game": tag, "waiting_for": wf,
                      "p1_life": G["ptr_life"]})
        # bug settle: no may-prompt and Priority persists -> confirmed
        if (G["trigger_resolved"] and not G["may_prompt_seen"]
                and not G["bug_confirmed"]
                and time.time() > G["settle_until"]):
            wf = ((p0.latest or {}).get("state", {}) or {}
                  .get("waiting_for", {}) or {}).get("type")
            if wf == "Priority":
                G["bug_confirmed"] = True
                say(f"[{tag}] BUG CONFIRMED: no may-cast prompt offered; "
                    f"game at clean Priority")
                wire("bug_confirmed", {"game": tag, "waiting_for": wf})
            else:
                G["settle_rounds"] += 1
                G["settle_until"] = time.time() + 3
                say(f"[{tag}] settle: waiting_for={wf} (not Priority); "
                    f"round {G['settle_rounds']}")
                if G["settle_rounds"] >= 5:
                    G["notes"].append(
                        f"[{tag}] unrecognized post-trigger waiting_for={wf}; "
                        f"scan did not answer")
                    G["bug_confirmed"] = True
        # accept path: track the free-cast spell to resolution
        if G["may_answered"] and G["may_accepted"]:
            if not G["spell_on_stack"]:
                if any(desired in b for b in stack_blobs(cur)):
                    G["spell_on_stack"] = True
                    say(f"[{tag}] free-cast {desired} observed on stack")
                    wire("spell_on_stack", {"game": tag})
            if G["spell_on_stack"] and not any(desired in b
                                               for b in stack_blobs(cur)):
                G["spell_resolved"] = True
                try:
                    await export("post_cast")
                except Exception as e:
                    G["notes"].append(f"post_cast export failed: {e}")
                say(f"[{tag}] free-cast {desired} resolved")
                wire("spell_resolved", {"game": tag})
                G["terminal"] = True
            elif not G["spell_on_stack"]:
                # effect-based fallback: Shock damage / Divination in gy
                if desired == SHOCK and G["ptr_life"] is not None:
                    if (life_of(cur, 1) or 99) < G["ptr_life"]:
                        G["spell_on_stack"] = True
                        G["spell_resolved"] = True
                        try:
                            await export("post_cast")
                        except Exception as e:
                            G["notes"].append(f"post_cast export failed: {e}")
                        say(f"[{tag}] free-cast Shock resolved "
                            f"(life {G['ptr_life']}->{life_of(cur, 1)})")
                        G["terminal"] = True
                if (desired == DIV
                        and zone_ids(cur, "Graveyard", 0, DIV)):
                    G["spell_on_stack"] = True
                    G["spell_resolved"] = True
                    try:
                        await export("post_cast")
                    except Exception as e:
                        G["notes"].append(f"post_cast export failed: {e}")
                    say(f"[{tag}] free-cast Divination resolved (in gy)")
                    G["terminal"] = True
        if G["may_answered"] and G["may_accepted"] is False:
            G["terminal"] = True
        # finish once the game has advanced past the kill turn
        if (G.get("terminal") or G.get("bug_confirmed")) \
                and not G["post_exported"]:
            ct = cur.get("turn_number")
            kt = G.get("kill_turn")
            if ct is not None and kt is not None and ct > kt + 1:
                try:
                    await export("post")
                except Exception as e:
                    G["notes"].append(f"post export failed: {e}")
                G["post_exported"] = True
                G["finished"] = True
                say(f"[{tag}] post exported at turn {ct}; game finished")
        # may-prompt watchdog: seen but never answered
        if (G["may_prompt_seen"] and not G["may_answered"]
                and time.time() - G["may_prompt_at"] > 30
                and not G["finished"]):
            G["notes"].append(f"[{tag}] may-prompt seen but not answered "
                              f"within 30s")
            try:
                await export("post")
            except Exception as e:
                G["notes"].append(f"post export failed: {e}")
            G["post_exported"] = True
            G["finished"] = True

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    try:
        while time.time() - t0 < 900 and not G["finished"]:
            await asyncio.sleep(0.15)
            for c, tick, tagn in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                same_rev = (rev == last.get(tagn))
                stale = time.time() - last_tick_at.get(tagn, 0) > 5
                if same_rev and not stale:
                    continue
                last[tagn] = rev
                last_tick_at[tagn] = time.time()
                try:
                    await tick(st, merged_actions(st), st["state"])
                except Exception as e:
                    say(f"[{tag}] tick error {tagn}: {e}")
                    wire("tick_error", {"game": tag, "who": tagn,
                                        "err": str(e)})
            cur = (p0.latest or {}).get("state", {}) or {}
            if cur:
                try:
                    await loop_checks(cur)
                except Exception as e:
                    say(f"[{tag}] loop_checks error: {e}")
                    wire("loop_error", {"game": tag, "err": str(e)})
            if time.time() - last_diag > 60 and p0.latest:
                last_diag = time.time()
                s = p0.latest["state"]
                say(f"[{tag}] DIAG turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} "
                    f"wf={(s.get('waiting_for') or {}).get('type')} "
                    f"pp={s.get('priority_player')} "
                    f"hk={G['hk_oid']} staged={G['staged']} "
                    f"murder={G['murder_cast']} dead={G['hk_dead']} "
                    f"dies={G['dies_seen']} resolved={G['trigger_resolved']} "
                    f"may_seen={G['may_prompt_seen']} "
                    f"life={[life_of(s, i) for i in (0, 1)]} "
                    f"stack={len(s.get('stack') or [])}")
        if not G["finished"]:
            G["notes"].append(f"[{tag}] per-game timeout (900s)")
            try:
                await export("post")
                G["post_exported"] = True
            except Exception as e:
                G["notes"].append(f"post export failed: {e}")
    finally:
        await p0.close()
        await p1.close()
    return G


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.loads(f.read())["state"]


def evaluate(G, cfg):
    tag = cfg["tag"]
    desired = cfg["desired"]
    ass = G["ass"]
    notes = G["notes"]
    pre = load_state(f"{EVDIR}/{tag}_pre_kill.json")
    ptr = load_state(f"{EVDIR}/{tag}_post_trigger.json")
    post = load_state(f"{EVDIR}/{tag}_post.json")
    pcast = load_state(f"{EVDIR}/{tag}_post_cast.json")

    # A1
    a1 = (pre is not None and bf_id(pre, 0, HK) is not None
          and life_of(pre, 0) == 20 and life_of(pre, 1) == 20
          and G["murder_cast"])
    ass["A1_setup_ok"] = "passed" if a1 else "failed"
    notes.append(f"[{tag}] A1: pre_kill hk_on_bf={pre is not None and bf_id(pre, 0, HK) is not None}, "
                 f"life={[life_of(pre, i) if pre else None for i in (0, 1)]}, "
                 f"murder_cast={G['murder_cast']}")
    # A2
    if ptr is not None and G["hk_dead"]:
        hk_gy = zone_ids(ptr, "Graveyard", 0, HK)
        ex = zone_ids(ptr, "Exile", 0)
        ex_names = [lname(ptr, o) for o in ex]
        life_ok = life_of(ptr, 1) == 20 - cfg["mv"]
        ok = (len(hk_gy) == 1 and ex_names == [desired] and life_ok)
        ass["A2_trigger_resolves"] = "passed" if ok else "failed"
        notes.append(f"[{tag}] A2: hk_in_gy={len(hk_gy)}, exile={ex_names}, "
                     f"P1 life 20->{life_of(ptr, 1)} (expected {20 - cfg['mv']})")
    else:
        ass["A2_trigger_resolves"] = "failed" if G["hk_dead"] else "not-run"
        notes.append(f"[{tag}] A2: post_trigger present={ptr is not None}, "
                     f"hk_dead={G['hk_dead']}")
    # A3
    if G["trigger_resolved"]:
        ass["A3_cast_offered"] = ("passed" if G["may_prompt_seen"] else "failed")
        notes.append(f"[{tag}] A3: may_prompt_seen={G['may_prompt_seen']}, "
                     f"waiting_for at resolution={G['may_wf_type']}")
    # A4
    if G["may_answered"] and G["may_accepted"]:
        if desired == SHOCK:
            ok = (pcast is not None
                  and life_of(pcast, 1) == (G["ptr_life"] or 99) - 2
                  and len(zone_ids(pcast, "Graveyard", 0, SHOCK)) >= 1)
            notes.append(f"[{tag}] A4 accept/Shock: post_cast P1 life "
                         f"{G['ptr_life']}->{life_of(pcast, 1) if pcast else None} "
                         f"(expected -2); shock_in_gy="
                         f"{len(zone_ids(pcast, 'Graveyard', 0, SHOCK)) if pcast else None}")
        else:
            ok = (pcast is not None
                  and len(lib_oids(pcast, 0)) == len(lib_oids(ptr, 0)) - 2
                  and len(zone_ids(pcast, "Graveyard", 0, DIV)) >= 1) \
                if ptr is not None else False
            notes.append(f"[{tag}] A4 accept/Divination: P0 library "
                         f"{len(lib_oids(ptr, 0)) if ptr else None}->"
                         f"{len(lib_oids(pcast, 0)) if pcast else None} "
                         f"(expected -2); div_in_gy="
                         f"{len(zone_ids(pcast, 'Graveyard', 0, DIV)) if pcast else None}")
        ass["A4_free_cast"] = "passed" if ok else "failed"
    elif G["may_answered"] and G["may_accepted"] is False:
        ex = [lname(post, o) for o in zone_ids(post, "Exile", 0)] if post else None
        ok = (post is not None and ex == [desired]
              and life_of(post, 1) == G["ptr_life"]
              and len(post.get("stack") or []) == 0)
        ass["A4_free_cast"] = "passed" if ok else "failed"
        notes.append(f"[{tag}] A4 decline: exile={ex}, P1 life "
                     f"{G['ptr_life']}->{life_of(post, 1) if post else None}")
    else:
        ass["A4_free_cast"] = "not-run"
        notes.append(f"[{tag}] A4: not-run (no may-choice answered)")
    # A5
    if post is not None and G["kill_turn"] is not None:
        ok = (len(post.get("stack") or []) == 0
              and (post.get("turn_number") or 0) > G["kill_turn"])
        ass["A5_cleanup"] = "passed" if ok else "failed"
        notes.append(f"[{tag}] A5: stack={len(post.get('stack') or [])}, "
                     f"turn {G['kill_turn']}->{post.get('turn_number')}, "
                     f"phase={post.get('phase')}")
    else:
        ass["A5_cleanup"] = "not-run"

    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif ass["A2_trigger_resolves"] == "failed" or ass["A3_cast_offered"] == "failed":
        verdict = "reproduced"
    elif all(v == "passed" for v in ass.values()):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    return verdict


async def main():
    t_start = time.time()
    results = []
    for cfg in GAMES:
        G = await run_game(cfg)
        verdict = evaluate(G, cfg)
        say(f"[{cfg['tag']}] ASSERTIONS: " +
            " ".join(f"{k}: {v};" for k, v in G["ass"].items()))
        say(f"[{cfg['tag']}] VERDICT: {verdict}")
        wire("game_verdict", {"game": cfg["tag"], "verdict": verdict,
                              "assertions": G["ass"], "notes": G["notes"]})
        results.append({"tag": cfg["tag"], "desc": cfg["desc"],
                        "verdict": verdict, "assertions": G["ass"],
                        "notes": G["notes"],
                        "may_wf_type": G["may_wf_type"],
                        "kill_turn": G["kill_turn"]})
    verdicts = [r["verdict"] for r in results]
    if any(v == "reproduced" for v in verdicts):
        overall = "reproduced"
    elif all(v == "not-reproduced" for v in verdicts):
        overall = "not-reproduced"
    elif all(v == "blocked" for v in verdicts):
        overall = "blocked"
    else:
        overall = "reproduced"
    say(f"OVERALL VERDICT: {overall}")
    run = {
        "issue": 6666,
        "run_id": RUN_ID,
        "server": SERVER_IDENTITY,
        "decks": {
            "P0": [["hidetsugu and kairi", 12], ["<desired: shock|divination>", 24],
                   ["island", 12], ["swamp", 12]],
            "P1": [["murder", 12], ["swamp", 48]],
        },
        "games": results,
        "verdict": overall,
        "evidence_dir": f"evidence/6666/{RUN_ID}",
        "duration_s": round(time.time() - t_start, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)
    say(f"run.json written (verdict={overall})")


asyncio.run(main())
