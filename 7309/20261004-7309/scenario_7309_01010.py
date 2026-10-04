#!/usr/bin/env python3
"""Issue #7309: Baxter Building "{4}, {T}: Add four mana in any combination
of colors" only allows four mana of a single color (no mixing).

Re-run on pinned v0.101.0 / protocol 103 (run 20261004-7309). Scenario flow
identical to scenario_7309.py (run 20261001-7309), which returned
reproduced on v0.99.0/protocol 98 (A1..A5 passed; published,
evidence 7309/20261001-7309).

Protocol-103 notes (from the 2026-10-03/04 #301/#647/#650/#658/#818 runs):
HELLO advertises protocol 103 (exact match enforced); MulliganDecision as
{"choice":{"type":"Keep"}} gated on waiting_for.data.pending[] Declare;
BottomCards defensive; DiscardToHandSize via the advertised
viewer_interaction opportunity (legacy SelectCards is silently ignored on
103); ActivateAbility submitted as advertised (source_id/ability_index);
PassPriority legacy first, then the viewer_interaction passPriority
action-code fallback (vi is persistently canSubmit on 103); revision-gated
main loop + 5s re-tick safety net for a client holding priority with no
revision change; export-only checkpoints fall through to the priority pass
(never return while holding priority).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, confirmed): Baxter Building's "{4}, {T}: Add four mana in
any combination of colors" only allows four mana of a single color, not
mix and match as intended.

Setup (native engine, two human driver seats):
  P0: 4x Baxter Building + 56x Mountain. Plays Baxter, then Mountains.
  P1: 60x Forest (driven: keep, land, pass; never attacks).

Trigger: on P0's main phase with Baxter untapped and >=4 untapped
Mountains, P0 holding priority, activate Baxter's ability_index 1
(the any-combination mana ability), choosing the White pre-expansion
(the reported branch).

Expected (per card text): the engine offers a genuine any-combination
choice (e.g. a manaGroups prompt) so the 4 mana can be split across colors.
Reported bug: the engine pre-expands ability 1 into 5 single-color
TapLandForMana options (W/U/B/R/G, output Concrete <color>), with no mix
opportunity at any point; the pool ends up 4x one color.

Assertions:
  A1_setup_ok      pre.json: P0 main phase, Baxter untapped on BF,
                   >=4 untapped Mountains, P0 holds priority
  A2_activation_ok White ability-1 submitted and accepted (no rejection);
                   {4} paid (4 Mountains tapped); Baxter tapped
  A3_single_color_4 post.json: P0 pool is exactly 4 mana, all White,
                   all sourced from the activated Baxter
  A4_no_mix_offered the recorded ability-1 offers were only single-color
                   pre-expansions, and no manaGroups/any-combination choice
                   was ever advertised via viewer_interaction
  A5_cleanup       no dangling decisions; game proceeds (waiting_for Priority)

Verdict: reproduced iff A1+A2 pass and A3+A4 hold. not-reproduced iff a
genuine mix choice is offered and the pool reflects a mixed selection.
blocked iff the setup cannot be driven to completion.

The browser mana-choice UI is NOT exercised; the engine-level
pre-expansion into 5 single-color taps is the defect surface under test.
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
RUN_ID = "20261004-7309"

SERVER_IDENTITY = {
    "server_version": "0.101.0",
    "build_commit": "acafe9b",
    "protocol_version": 103,
    "mode": "Full",
    "binary_sha256": "c32eabdcf93d04f61186558c223a12e6edbe15a678050863ae6d535165359b0a",
    "card_data_sha256": "b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00",
    "draft_pools_sha256": "75bb313864c341a99747e2a2a446dda5d83763bf073f5215d39289fdf8d34b06",
    "signature_verified": True,
    "source": "ledger server pin (v0.101.0, minisign-verified; binary + data hashes "
              "recomputed against on-disk artifacts below; live ServerHello probe at run start "
              "(0.101.0/acafe9b/103/Full) on the backfill-owned 127.0.0.1:9374 server)",
}


def _sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


for _f, _k in (("server/releases/v0.101.0/phase-server-slim-x86_64-unknown-linux-musl", "binary_sha256"),
               ("server/releases/v0.101.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.101.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

EVDIR = f"{BACKFILL}/evidence/7309/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BAXTER = "Baxter Building"
MOUNTAIN = "Mountain"
FOREST = "Forest"
P0_DECK = [(BAXTER, 4), (MOUNTAIN, 56)]
P1_AI_DECK = [(FOREST, 60)]
LANDS = (MOUNTAIN, BAXTER)
TIMEOUT = 1500

ST = {}
MULLS = {}
WF_SEEN = []
C0 = None


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "pre_exported": False, "mid_exported": False, "post_exported": False,
        "offers_exported": False,
        "mix_choice_seen": False, "mix_choice_offer": None,
        "mix_submitted": False, "mix_accepted": None,
        "white_submitted": False, "white_accepted": None,
        "rejections": [], "stop": False,
        "baxter_oid": None, "baxter_tapped_pre": None,
        "mountains_tapped_pre": None,
        "pool_after": None,
        "act_submitted_at": None,
        "hold_priority": False,
        "mountain_tap_iids": set(),
        "last_act_diag": 0,
    })
    MULLS.clear()
    MULLS.update({"P0": 0})
    WF_SEEN.clear()


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def oname(state, oid):
    o = state.get("objects", {}).get(str(oid), {})
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def hand_ids(state, pid):
    for pl in state.get("players", []):
        if str(pl.get("id")) == str(pid):
            return [int(o) for o in (pl.get("hand") or [])]
    return []


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if oname(state, oid) == name:
            return str(oid)
    return None


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if (o.get("card_name") or o.get("name") or o.get("base_name")) == name
               and not o.get("tapped"))


def baxter_untapped_oid(state, pid):
    for oid, o in bf(state, pid):
        if (o.get("card_name") or o.get("name") or o.get("base_name")) == BAXTER \
                and not o.get("tapped"):
            return str(oid)
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def pending_for(state, pid):
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    return wf_type(state) == "Priority" and str(wf_player(state)) == str(pid)


def get_vi(st):
    return st.get("viewer_interaction") or {}


def vi_opportunities(st):
    vi = get_vi(st)
    opps = vi.get("opportunities") or []
    # tolerate alternate shapes
    single = vi.get("opportunity")
    if single and not opps:
        opps = [single]
    return opps


async def submit_as_is(c, a):
    # Unit variants (e.g. PassPriority) are advertised with NO data key;
    # echoing one back with "data": {} is rejected by the deserializer.
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg, "stage": ST.get("stage")})
    await c.send_action(msg)


def submit_by_object_action(c, p):
    """Submit a per-object payload as the engine-authored GameAction:
    keep type + data, strip envelope keys (e.g. interactionActionId)."""
    msg = {"type": p["type"]}
    if "data" in p:
        msg["data"] = p["data"]
    return msg


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error", "InteractionRejected"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:400]}")
    return found


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


async def do_mulligan(c, pid):
    """Protocol-103 MulliganDecision: single decision per player (permanent
    guard like scenario_818i.py). Always Keep - P0 draws into Baxter
    (4 copies) if not in the opener."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if MULLS.get(("decided", c.name)):
        return False
    MULLS[("decided", c.name)] = True
    n_lands = sum(1 for o in hand_ids(state, pid)
                  if oname(state, o) in LANDS)
    has_bax = find_hand(state, pid, BAXTER) is not None
    wire("mulligan", {"who": c.name, "decision": "Keep",
                      "has_baxter": has_bax, "n_lands": n_lands})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    say(f"{c.name} mulligan -> Keep (baxter={has_bax} lands={n_lands})")
    return True


async def do_bottom(c, pid):
    """BottomCards phase after a mulligan: bottom the required count via
    SelectCards, preferring non-lands (keep Baxter for P0)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "BottomCards":
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    if MULLS.get(("bottomed", c.name)):
        return False
    MULLS[("bottomed", c.name)] = True
    ph = pend.get("phase") or {}
    n = int(ph.get("count", 1) or 1)
    h = hand_ids(state, pid)
    keep_bax = find_hand(state, pid, BAXTER)
    # bottom non-lands first, then lands, never the Baxter we want
    pref = [o for o in h if str(o) != str(keep_bax) and oname(state, o) not in LANDS]
    pref += [o for o in h if str(o) not in [str(x) for x in pref] and str(o) != str(keep_bax)]
    picks = [int(x) for x in pref[:n]]
    wire("bottom_cards", {"who": c.name, "n": n, "picks": picks})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}: {picks}")
    return True


async def do_discard(c, pid):
    """Protocol-103 DiscardToHandSize via the advertised viewer_interaction
    opportunity. The legacy SelectCards action is silently ignored on 103."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    for op in vi_opportunities(st):
        iid = op.get("interactionId") or op.get("id")
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands or not iid:
            continue
        picks = [ch.get("id") for ch in cands[:n] if isinstance(ch, dict)]
        # prefer discarding non-lands, keep a Baxter if we have one
        keep_bax = find_hand(state, pid, BAXTER)
        wire("discard_interaction", {"who": c.name, "iid": iid,
                                     "n": n, "picks": picks})
        spec = data.get("spec") or {}
        rtype = spec.get("type") or ("choose" if resp.get("type") == "exactChoices"
                                     else "select")
        await c.send_interaction({"interactionId": iid,
                                  "response": {"type": rtype,
                                               "data": {"choiceId": picks[0]} if rtype == "choose"
                                               else {"choiceIds": picks}}})
        say(f"{c.name} discards {n} to hand size via interaction")
        return True
    wire("discard_no_opportunity", {"who": c.name})
    return False


async def pass_prio(c, acts):
    """Pass priority: legacy PassPriority action first, then the protocol-103
    viewer_interaction passPriority action-code fallback."""
    st = c.latest
    # post-evidence hold: freeze priority until post.json is captured
    if ST.get("hold_priority") and not ST.get("post_exported"):
        return False
    if ST.get("stage") == "ACTIVATED" and not ST.get("post_exported"):
        pool = pool_of(st["state"], c.player_id)
        mana = (pool or {}).get("mana", []) if isinstance(pool, dict) else []
        if len(mana) >= 4:
            ST["hold_priority"] = True
            ST["pool_after"] = pool
            wire("pool_after_hold", {"pool": pool})
            say("hold: pool observed, freezing priority for post export")
            return False
    if not my_priority(st["state"], c.player_id):
        return False
    rev = st.get("state_revision", -1)
    if ST.get(("passed", c.name, rev)):
        return False
    for a in acts:
        if a.get("type") == "PassPriority":
            ST[("passed", c.name, rev)] = True
            await submit_as_is(c, a)
            return True
    for op in vi_opportunities(st):
        data = (op.get("response") or {}).get("data") or {}
        for ch in data.get("choices") or []:
            codes = [s2.get("data", {}).get("code")
                     for s2 in ch.get("surfaces", []) or []
                     if s2.get("type") == "action"]
            if "passPriority" in codes:
                iid = op.get("interactionId") or op.get("id")
                say(f"[{c.name}] passPriority via interaction fallback")
                wire("pass_prio_fallback", {"who": c.name, "iid": iid})
                await c.send_interaction(
                    {"interactionId": iid,
                     "response": {"type": "choose", "data": {"choiceId": ch.get("id")}}})
                return True
    return False


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": (wf_of(state).get("data") or {}),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} stage={ST.get('stage')}")


def pool_of(state, pid):
    for p in state.get("players", []):
        if str(p.get("id")) == str(pid):
            return p.get("mana_pool") or p.get("manaPool")
    return None


def find_baxter_ability1_offers(st, state, pid):
    """Find engine-advertised per-object actions for Baxter Building's
    '{4},{T}: Add four mana in any combination' (ability_index 1).

    v0.99.0 model: ability 1 pre-expanded into 5 single-color
    TapLandForMana selections (W/U/B/R/G), each output Concrete <color>.
    Record the full offered set for A4; return the White candidate (the
    reported branch) or the first candidate."""
    lafo = st.get("legal_actions_by_object") or {}
    boids = [str(oid) for oid, o in bf(state, pid)
             if (o.get("card_name") or o.get("name") or o.get("base_name")) == BAXTER]
    cands = []
    for k, payloads in lafo.items():
        if str(k) not in boids:
            continue
        for p in payloads or []:
            if not isinstance(p, dict):
                continue
            if p.get("type") not in ("ActivateManaSource", "TapLandForMana",
                                     "ActivateAbility"):
                continue
            sel = (p.get("data") or {}).get("selection") or {}
            if sel.get("ability_index") == 1:
                cands.append(p)
    if cands:
        wire("baxter_ability1_options",
             [{"mana_type": ((p.get("data") or {}).get("selection") or {}).get("mana_type"),
               "output": ((p.get("data") or {}).get("selection") or {}).get("output"),
               "type": p.get("type"),
               "top_keys": sorted(p.keys())} for p in cands])
        for p in cands:
            sel = (p.get("data") or {}).get("selection") or {}
            if sel.get("mana_type") == "White":
                return p
        return cands[0]
    return None


def scan_mix_choice(st):
    """Scan viewer_interaction opportunities for any any-combination /
    manaGroups mix choice. Returns the opportunity or None. This is the
    'not-reproduced' signal: the engine offering a genuine mix choice."""
    for op in vi_opportunities(st):
        blob = json.dumps(op, default=str).lower()
        iid = op.get("interactionId") or op.get("id")
        resp = op.get("response", {}) or {}
        spec = (resp.get("data", {}) or {}).get("spec") or {}
        if spec.get("type") == "manaGroups":
            return op
        if "any combination" in blob or "mix" in blob:
            # exclude our own passPriority fallback surface
            if "passpriority" in blob and len(blob) < 600:
                continue
            return op
    return None


async def handle_mix_choice(c, op):
    """A genuine mix choice appeared: record it and submit a mixed
    selection (W/U/B/R). Returns True if submitted."""
    iid = op.get("interactionId") or op.get("id")
    resp = op.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    cands = data.get("candidates") or data.get("choices") or []
    if not ST["mix_choice_seen"]:
        ST["mix_choice_seen"] = True
        ST["mix_choice_offer"] = {"interactionId": iid, "spec": spec,
                                  "candidates": cands}
        wire("mix_choice_offered", {"interactionId": iid, "spec": spec,
                                    "candidates": cands})
        say(f"MIX CHOICE offered: spec={json.dumps(spec, default=str)[:500]}")
        say(f"candidates: {json.dumps(cands, default=str)[:800]}")
    if ST["mix_submitted"]:
        return False
    cmap = {}
    for cd in cands:
        if not isinstance(cd, dict):
            continue
        cid = cd.get("id")
        blob = json.dumps(cd, default=str).lower()
        for col in ("white", "blue", "black", "red", "green", "colorless"):
            if col in blob and cid is not None:
                cmap.setdefault(col, cid)
                break
    say(f"mix candidate color map: {cmap}")
    wire("mix_choice_candidates", {"map": cmap})
    want = ["white", "blue", "black", "red"]
    ids = [cmap[w] for w in want if w in cmap]
    if len(ids) == 4:
        rtype = spec.get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": rtype, "data": {"choiceIds": ids}}}
        wire("mix_choice_submit", sub)
        await c.send_interaction(sub)
        ST["mix_submitted"] = True
        say(f"submitted MIXED mana choice {want} via {rtype}")
        return True
    say("cannot build a 4-color mixed choice from candidates; leaving unsubmitted")
    wire("mix_choice_unbuildable", {"map": cmap})
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = st.get("legal_actions", []) or []
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    # mana payment prompts: submit as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            say(f"[{c.name}] submitted {a['type']} as-is")
            return True
    # A4 watch: any mix choice advertised while the ability is live?
    if ST["stage"] in ("SETUP", "ACTIVATED"):
        mix_op = scan_mix_choice(st)
        if mix_op:
            if await handle_mix_choice(c, mix_op):
                return True
    # activation window
    if ST["stage"] == "SETUP" and is_my_main(state, pid) and my_priority(state, pid):
        bo = baxter_untapped_oid(state, pid)
        nm = untapped_of(state, pid, MOUNTAIN)
        if bo and nm >= 4:
            if not ST["pre_exported"]:
                ST["baxter_oid"] = bo
                ST["mountains_tapped_pre"] = sum(
                    1 for _, o in bf(state, pid)
                    if (o.get("card_name") or o.get("name") or o.get("base_name")) == MOUNTAIN
                    and o.get("tapped"))
                if await export_now("pre.json") is not None:
                    ST["pre_exported"] = True
                    wire("pre", {"baxter_oid": bo, "untapped_mountains": nm,
                                 "turn": state.get("turn_number"),
                                 "phase": state.get("phase")})
            a = find_baxter_ability1_offers(st, state, pid)
            if a:
                # record the full offer set for A4
                if not ST["offers_exported"]:
                    lafo = st.get("legal_actions_by_object") or {}
                    act_src = str((((a.get("data") or {}).get("selection") or {}).get("source") or {}).get("object_id"))
                    bax_offers = {}
                    for k, payloads in lafo.items():
                        for p in payloads or []:
                            if not isinstance(p, dict):
                                continue
                            sel = (p.get("data") or {}).get("selection") or {}
                            src = (sel.get("source") or {}).get("object_id")
                            if str(src) == act_src and sel.get("ability_index") == 1:
                                d = p.get("data") or {}
                                bax_offers.setdefault(str(k), []).append({
                                    "action_type": p.get("type"),
                                    "mana_type": sel.get("mana_type"),
                                    "ability_index": sel.get("ability_index"),
                                    "output": d.get("output") or sel.get("output"),
                                })
                    with open(f"{EVDIR}/activation_offers.json", "w") as f:
                        json.dump({"baxter_oid": act_src, "ability_1_offers": bax_offers,
                                   "offer_count": sum(len(v) for v in bax_offers.values())},
                                  f, default=str, indent=1)
                    ST["offers_exported"] = True
                    wire("activation_offers",
                         {"offer_count": sum(len(v) for v in bax_offers.values()),
                          "colors": sorted({o["mana_type"] for v in bax_offers.values() for o in v})})
                    say(f"recorded {sum(len(v) for v in bax_offers.values())} ability-1 offers")
                sub = submit_by_object_action(c, a)
                src_oid = (((sub.get("data") or {}).get("selection") or {}).get("source") or {}).get("object_id")
                wire("baxter_activation_submit", {"action": sub})
                await c.send_action(sub)
                ST["act_submitted_at"] = time.time()
                ST["stage"] = "ACTIVATED"
                ST["baxter_oid"] = str(src_oid or bo)
                ST["white_submitted"] = True
                say(f"[P0] activated Baxter mana ability (oid={ST['baxter_oid']}) type={a['type']}")
                return True
            wire("no_baxter_action", {
                "flat_types": sorted(set(x.get("type") for x in acts)),
                "byobj_keys": len(st.get("legal_actions_by_object") or {}),
            })
    # after activation: pay {4} via advertised PayMana actions, else tap
    # untapped Mountains through per-object TapLandForMana (ability_index 0)
    if ST["stage"] == "ACTIVATED":
        lafo = st.get("legal_actions_by_object") or {}
        done_iids = ST["mountain_tap_iids"]
        tapped_m = sum(1 for _, o in bf(state, pid)
                       if (o.get("card_name") or o.get("name") or o.get("base_name")) == MOUNTAIN
                       and o.get("tapped"))
        if tapped_m < 4:
            for k, payloads in lafo.items():
                if tapped_m >= 4:
                    break
                for p in payloads or []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") != "TapLandForMana":
                        continue
                    iid = p.get("interactionActionId")
                    if iid in done_iids:
                        continue
                    sel = (p.get("data") or {}).get("selection") or {}
                    if sel.get("ability_index") != 0:
                        continue
                    src_oid = str((sel.get("source") or {}).get("object_id"))
                    o = state.get("objects", {}).get(src_oid)
                    if not o or (o.get("card_name") or o.get("name") or o.get("base_name")) != MOUNTAIN \
                            or o.get("tapped"):
                        continue
                    sub = submit_by_object_action(c, p)
                    wire("mountain_tap", {"oid": src_oid, "iid": iid, "n": tapped_m + 1})
                    await c.send_action(sub)
                    done_iids.add(iid)
                    say(f"[P0] tapped Mountain {src_oid} toward {{4}} ({tapped_m + 1}/4)")
                    return True
        # mid export once the Baxter is tapped
        _mid_obj = state.get("objects", {}).get(str(ST.get("baxter_oid") or "")) or {}
        if _mid_obj.get("tapped") and not ST["mid_exported"]:
            if await export_now("mid_tapped.json") is not None:
                ST["mid_exported"] = True
                wire("mid_tapped", {"turn": state.get("turn_number"),
                                    "phase": state.get("phase"),
                                    "pool": pool_of(state, pid),
                                    "waiting_for": wf_of(state)})
    # setup play: Baxter first, then lands
    if is_my_main(state, pid) and ST["stage"] == "SETUP":
        for name in (BAXTER, MOUNTAIN):
            lid = find_hand(state, pid, name)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        say(f"[P0] plays {name}")
                        return True
    # never attack; empty DeclareAttackers
    if state.get("active_player") == pid and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                import copy as _copy
                sub = _copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
                say("[P0] declares no attackers")
                return True
    # fall through to the priority pass (never return after an export
    # while holding priority)
    if await pass_prio(c, acts):
        return True
    return False


async def p1_tick(c, pid):
    """Minimal P1 driver: mulligan keep, play a Forest, never attack,
    pass priority. (The native Easy AI seat stalled at MulliganDecision
    on v0.101.0; two human seats are the robust pattern.)"""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    acts = st.get("legal_actions", []) or []
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if wf_type(state) == "DeclareAttackers":
        for a in acts:
            if a["type"] == "DeclareAttackers":
                import copy as _copy
                sub = _copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
                say("[P1] declares no attackers")
                return True
        return False
    if wf_type(state) == "DeclareBlockers":
        for a in acts:
            if a["type"] == "DeclareBlockers":
                import copy as _copy
                sub = _copy.deepcopy(a)
                sub["data"]["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                say("[P1] declares no blockers")
                return True
        return False
    if wf_type(state) != "Priority" or not my_priority(state, pid):
        if wf_type(state) == "OrderTriggers":
            for a in acts:
                if a["type"] == "OrderTriggers":
                    await submit_as_is(c, a)
                    say("[P1] submits trigger order as advertised")
                    return True
        return False
    # play a land
    for a in acts:
        if a["type"] == "PlayLand":
            await submit_as_is(c, a)
            return True
    if await pass_prio(c, acts):
        return True
    return False


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_AI_DECK))
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 seat={p1.player_id} (human driver)")
    wire("game_created", {"code": p0.game_code, "p0_seat": p0.player_id,
                          "p1_seat": p1.player_id,
                          "p0_deck": P0_DECK, "p1_deck": P1_AI_DECK})

    last_rev = {}
    last_tick_at = {}
    last_send_at = {}
    clients = [(p0, p0.player_id, tick), (p1, p1.player_id, p1_tick)]
    for c, _, _ in clients:
        last_rev[c.name] = -1
        last_tick_at[c.name] = 0.0
        last_send_at[c.name] = 0.0
        orig_send = c.send_action

        async def tracked_send(action, _c=c, _orig=orig_send):
            last_send_at[_c.name] = time.time()
            await _orig(action)
        c.send_action = tracked_send
        orig_interact = c.send_interaction

        async def tracked_interact(sub, _c=c, _orig=orig_interact):
            last_send_at[_c.name] = time.time()
            await _orig(sub)
        c.send_interaction = tracked_interact

    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        now = time.time()
        for c, pid, tickfn in clients:
            rej = drain_rejections(c)
            if rej:
                ST["rejections"].extend(rej)
                for r in rej:
                    if ST.get("white_submitted") and ST.get("white_accepted") is None:
                        ST["white_accepted"] = False
                        say("WHITE ability-1 submission REJECTED")
                        wire("white_rejected", r)
                    if ST.get("mix_submitted") and ST.get("mix_accepted") is None:
                        ST["mix_accepted"] = False
                        say("MIXED mana choice REJECTED")
                        wire("mix_rejected", r)
        # single yield before evaluation (protocol-103 leg-engagement lesson)
        await asyncio.sleep(0)
        for c, pid, tickfn in clients:
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last_rev[c.name])
            # re-tick safety net: holding priority with no revision change >5s
            due_retry = (same_rev and last_send_at[c.name] > last_tick_at[c.name]
                         and now - last_send_at[c.name] > 5)
            if same_rev and not due_retry:
                continue
            last_rev[c.name] = rev
            last_tick_at[c.name] = now
            try:
                await tickfn(c, pid)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # ---- resolution capture: the moment the pool shows >=4 mana,
        # freeze priority and export post.json (fall-through design: the
        # export happens here, and tick()'s pass_prio honors the hold)
        if ST.get("stage") == "ACTIVATED" and not ST.get("post_exported"):
            _pool = pool_of(state, p0.player_id)
            _mana = (_pool or {}).get("mana", []) if isinstance(_pool, dict) else []
            if len(_mana) >= 4 or ST.get("hold_priority"):
                if _pool and len(_mana) >= 4 and not ST.get("pool_after"):
                    ST["pool_after"] = _pool
                    say(f"pool after activation: {json.dumps(_pool, default=str)[:400]}")
                    wire("pool_after", {"pool": _pool})
                ST["hold_priority"] = True
                try:
                    if await export_now("post.json") is not None:
                        ST["post_exported"] = True
                finally:
                    ST["hold_priority"] = False
                ST["stop"] = True

        # mix-choice path: if a mix choice was submitted and the pool is now
        # mixed, capture post and finish as not-reproduced
        if ST.get("mix_submitted") and not ST.get("post_exported"):
            _pool = pool_of(state, p0.player_id)
            _mana = (_pool or {}).get("mana", []) if isinstance(_pool, dict) else []
            if len(_mana) >= 4:
                ST["pool_after"] = _pool
                ST["hold_priority"] = True
                try:
                    if await export_now("post.json") is not None:
                        ST["post_exported"] = True
                finally:
                    ST["hold_priority"] = False
                ST["stop"] = True

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            say("game over")
            continue

        # stall guard: activation submitted but nothing happening for 90s
        if ST.get("white_submitted") and not ST.get("post_exported") \
                and now - (ST.get("act_submitted_at") or now) > 90:
            say("activation stall: 90s with no pool resolution; exporting post and stopping")
            wire("activation_stall", {"rejections": ST["rejections"][-3:]})
            ST["hold_priority"] = True
            try:
                await export_now("post.json")
            finally:
                ST["hold_priority"] = False
            ST["post_exported"] = True
            ST["stop"] = True

    await p0.close()
    return dict(ST)


if __name__ == "__main__":
    st = asyncio.run(main())
    print(json.dumps({k: (v if not isinstance(v, (dict, list)) else str(v)[:200])
                      for k, v in st.items()}, indent=2, default=str))
