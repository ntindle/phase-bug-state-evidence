#!/usr/bin/env python3
"""Issue #5936: "Game broke with a prio bug for something my friend couldn't
pay for" - Slinza, the Spiked Stampede.

Re-validation on pinned v0.85.0 (build cb58ef5, WS protocol 72).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-16, source: Discord thread 1525628325454676068): Slinza's
optional enters trigger reached a payment decision the affected player could
not pay for; the game broke and the player could not get past it. Error
messages appeared for both single-step "resolve" and "resolve all".

Oracle text (verified from pinned v0.85.0 card-data.json, exact name
"Slinza, the Spiked Stampede"):
  "Beast spells you cast cost {2} less to cast.
   Each other Beast creature you control enters with an additional +1/+1
   counter on it. Whenever Slinza or another creature with power 4 or greater
   enters, you may pay {1}{R/G}. When you do, Slinza fights target creature
   you don't control."

Triage acceptance criteria (mike-theDude, 2026-08-03):
  - If the cost cannot be paid, the player can decline and resolution
    continues.
  - If the cost is paid, only the conditional fight is created and it
    requires a legal opposing creature target.

Scenario (native engine, two human-client seats):
  P0: 12x Slinza, the Spiked Stampede + 12x Leatherback Baloth ({G}{G}{G}
      4/5 Beast) + 24x Forest (dense counts: engine accepts >4-of for custom
      games; mulligan hunts 2+ lands).
  P1: 12x Grizzly Bears + 36x Forest (plays land, casts Bears as fight
      targets; never attacks).

  PHASE A (reported path: cannot pay): P0 casts Slinza spending all 5 lands
      ({4}{G}), leaving 0 untapped mana when the ETB trigger fires. Accept
      the optional trigger, then DECLINE the {1}{R/G} payment (or let the
      engine auto-skip the unaffordable payment). Expected per acceptance
      criteria: the trigger resolves with no fight, no errors, no stall,
      and the game continues.

  PHASE B (control: can pay): P0 casts Leatherback Baloth while keeping 3+
      Forests untapped (Baloth {G}{G}{G} reduced by Slinza's {2} to {G});
      trigger fires; PAY {1}{R/G}; target P1's Grizzly Bears; fight
      resolves. Expected: Bears dies (5 damage), Slinza takes 2, life
      20/20, stack empty, game proceeds.

Assertions:
  A1_setup_ok     pre_A.json: Slinza on P0 BF, trigger on stack (Slinza
                  source), P0 has 0 untapped lands and empty pool.
  A2_cannot_pay   post_A.json: trigger resolved with no fight (Slinza
                  damage 0, no deaths attributable to a fight, life
                  20/20), stack empty, Priority, zero rejections on the
                  payment decision, stuck watchdog not fired.
  A3_can_pay      post_B.json: fight happened - P1 Grizzly Bears in
                  graveyard, Slinza marked with 2 damage, life 20/20,
                  stack empty, >=2 tapped-forest delta pre_B->post_B
                  (Baloth cast + trigger payment).
  A4_cleanup      final state: stack empty, game at Priority, no stall
                  watchdog fired.

Verdict rule: reproduced iff A1 passes and the cannot-pay branch errors
(submission rejected), stalls (no legal decline / watchdog fires), or the
game cannot proceed past the payment decision. not-reproduced iff all of
A1..A4 pass. blocked iff the game cannot be driven to a Slinza trigger.

Evidence: evidence/5936/<run-id>/pre_A.json, post_A.json, pre_B.json,
post_B.json, run.json, manifest.sha256, summary.png, scenario_5936_085.py,
wire_log.jsonl, scenario_run.log
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 5936
RUN_ID = "20260916-5936"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (#7174: bump RUN_ID)"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SLINZA = "slinza, the spiked stampede"
BALOTH = "leatherback baloth"
BEARS = "grizzly bears"
FOREST = "forest"

P0_DECK = [(SLINZA, 12), (BALOTH, 12), (FOREST, 24)]
P1_DECK = [(BEARS, 12), (FOREST, 36)]

# Verified 2026-09-16 against on-disk pinned release artifacts:
#   binary sha256 of server/releases/v0.85.0/phase-server-slim-x86_64-unknown-linux-musl
#   card-data.json / draft-pools.json under server/releases/v0.85.0/data/
SERVER_IDENTITY = {
    "server_version": "0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "mode": "Full",
    "binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
    "signature_verified": True,
    "observed_at": "2026-09-16",
    "source": "ServerHello + sha256 re-verified against pinned v0.85.0 "
              "release artifacts (binary+data under server/releases/v0.85.0/); "
              "fresh isolated server on 127.0.0.1:9375 for run 20260916-5936",
}

ACTED = set()
PROMPT_DONE = set()
REJECTS = {}
LAST_IID = {}
SKIP_IID = set()
ST = {}


def reset_per_game():
    """#5654: revisions restart at 0 per game; global guard sets must be
    reset per game or the second game's first actions get suppressed."""
    global ACTED, PROMPT_DONE, REJECTS, LAST_IID, SKIP_IID
    ACTED = set()
    PROMPT_DONE = set()
    REJECTS = {}
    LAST_IID = {}
    SKIP_IID = set()


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


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def untapped_forests(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and oname(o) == FOREST and not o.get("tapped")]


def tapped_forests(state, pid):
    return sum(1 for o in state.get("objects", {}).values()
               if oname(o) == FOREST and o.get("zone") == "Battlefield"
               and o.get("controller") == pid and o.get("tapped"))


def bf_creatures(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            tl = str(o.get("type_line") or "")
            if "creature" in tl.lower() or oname(o) in (SLINZA, BALOTH, BEARS):
                if name is None or oname(o) == name:
                    out.append(int(oid))
    return out


def damage_on(state, oid):
    return get_obj(state, oid).get("damage_marked", 0) or 0


def hand_oids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


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


def ref_of(ch):
    """Resolve a target candidate's object oid from its surfaces."""
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if not isinstance(d, dict):
            continue
        ref = d.get("reference")
        if ref is None:
            continue
        if isinstance(ref, dict):
            for k in ("object_id", "id"):
                if ref.get(k) is not None:
                    try:
                        return int(ref[k])
                    except (TypeError, ValueError):
                        pass
        else:
            try:
                return int(ref)
            except (TypeError, ValueError):
                pass
    return None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def value_pairs(ch):
    return [((s.get("data") or {}).get("role"),
             (s.get("data") or {}).get("value"))
            for s in ch.get("surfaces", []) or [] if s.get("type") == "value"]


def chs_of(opp):
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    return data.get("choices") or data.get("candidates") or []


def spec_type_of(opp):
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    spec = data.get("spec")
    if isinstance(spec, dict):
        return spec.get("type")
    return spec


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_IID[c.name] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            ST["rejections"] = ST.get("rejections", 0) + 1
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {body[:300]}")
            iid = LAST_IID.get(c.name)
            if iid and iid in body:
                REJECTS[iid] = REJECTS.get(iid, 0) + 1
                say(f"[{c.name}] iid {iid} rejection #{REJECTS[iid]}")
                if REJECTS[iid] >= 2 and iid not in SKIP_IID:
                    SKIP_IID.add(iid)
                    say(f"[{c.name}] iid {iid} rejected twice; skipping "
                        f"further submissions on it (logged, #4509)")
            else:
                wire("rejection_no_iid", {"who": c.name, "type": t,
                                         "data": data})


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED.add(k)
    return False


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_cannot_pay", "A3_can_pay", "A4_cleanup")}
    reset_per_game()
    ST.update({
        "phase": "A",
        "A_trigger_seen": False, "A_payment_offered": False,
        "A_declined": False, "A_auto_skipped": False, "A_done": False,
        "B_trigger_seen": False, "B_payment_offered": False,
        "B_paid": False, "B_target_chosen": None,
        "B_fight_resolved": False, "B_done": False,
        "stuck_watch_fired": False, "rejections": 0,
        "payment_path_A": None, "payment_path_B": None,
    })
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def log_vi(c, st, tag):
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return
        for opp in opps:
            resp = opp.get("response", {}) or {}
            info = {
                "tag": tag, "who": c.name,
                "interactionId": opp.get("interactionId"),
                "rtype": resp.get("type"), "spec": spec_type_of(opp),
                "prompt": str(opp.get("prompt") or opp.get("title") or "")[:160],
            }
            wire("vi_opportunity", info)
            say(f"[vi {tag}/{c.name}] iid={info['interactionId']} "
                f"rtype={info['rtype']} spec={info['spec']} "
                f"prompt={info['prompt'][:70]!r}")

    def pick_optional_effect_choice(chs, want_true):
        """decideOptionalEffect (accept) choice with value pair
        (accept, true|false); fall back to text match."""
        for ch in chs:
            if "decideOptionalEffect" in action_codes(ch):
                for role, val in value_pairs(ch):
                    if str(role).lower() == "accept" and \
                       str(val).lower() == ("true" if want_true else "false"):
                        return ch
        for ch in chs:
            t = (ch.get("text") or ch.get("label") or "").lower()
            if want_true and ("accept" in t or "yes" in t):
                return ch
            if not want_true and ("decline" in t or "no" in t):
                return ch
        return None

    def pick_optional_cost_choice(chs, want_pay):
        """decideOptionalCost choice with value pair (pay, true|false).
        Protocol 69+ carries EMPTY choice text (#6758); never match by text."""
        flag = ("pay", "true" if want_pay else "false")
        for ch in chs:
            if "decideOptionalCost" not in action_codes(ch):
                continue
            vals = [(str(r).lower(), str(v).lower()) for r, v in value_pairs(ch)]
            if flag in vals:
                return ch
        return None

    async def answer_discard(c, pid, opp, tag, protect):
        chs = chs_of(opp)
        state = c.latest["state"]
        d = (wf_of(state).get("data") or {})
        count = 1
        for k in ("count", "amount", "number"):
            if isinstance(d.get(k), int):
                count = d[k]
        oid_by_ref = {}
        for ch in chs:
            r = ref_of(ch)
            if r:
                oid_by_ref[r] = ch["id"]

        def rank(oid):
            nm = oname(get_obj(state, oid))
            if nm in protect:
                return (2, nm)
            if nm == FOREST:
                return (0, nm)
            return (1, nm)

        oids = sorted(hand_oids(state, pid), key=rank)[:count]
        picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
        if len(picks) < len(oids):
            say(f"[{tag}] discard: unmapped candidates; deferring")
            return False
        cids = [cid for _, cid in picks]
        rtype = (opp.get("response", {}) or {}).get("type")
        spec = spec_type_of(opp)
        if rtype == "schema" and spec in ("sequence", "select"):
            resp_out = {"type": spec, "data": {"choiceIds": cids}}
        elif rtype == "exactChoices":
            resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
        else:
            say(f"[{tag}] discard: unexpected rtype={rtype} spec={spec}; "
                f"deferring")
            return False
        await send_interaction(c, {"interactionId": opp.get("interactionId"),
                                   "response": resp_out})
        say(f"[{tag}] discard_to_hand_size answered ({len(cids)} cards)")
        return True

    async def handle_vi(c, pid, st, state):
        """Answer Slinza-trigger decision opportunities only. Plain priority
        menus are never answered here (#6758/#5936 lesson); they go through
        Action submissions."""
        vi = get_vi(st)
        if not vi:
            return False
        wtype = wf_of(state).get("type")
        wf_player = (wf_of(state).get("data") or {}).get("player")
        acted_flag = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in PROMPT_DONE or iid in SKIP_IID:
                continue
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            chs = chs_of(opp)
            # --- cleanup discard (named player only, #6690) ---
            if wtype == "DiscardToHandSize" and wf_player == pid:
                if await answer_discard(c, pid, opp, c.name, (SLINZA, BALOTH)):
                    PROMPT_DONE.add(iid)
                    acted_flag = True
                continue
            # --- optional "you may" trigger accept/decline ---
            if wtype == "OptionalEffectChoice":
                if pid != c.player_id and wf_player not in (None, pid):
                    continue
                want = None
                # always accept the Slinza optional trigger in both phases
                want = True
                ch = pick_optional_effect_choice(chs, want)
                if ch is None:
                    wire("optional_effect_no_choice",
                         {"who": c.name, "iid": iid, "phase": ST["phase"]})
                    say(f"[{c.name}] OptionalEffectChoice: no accept choice "
                        f"identified; deferring")
                    continue
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch["id"]}}})
                PROMPT_DONE.add(iid)
                say(f"[{c.name}] OptionalEffectChoice: accept trigger "
                    f"(phase {ST['phase']})")
                acted_flag = True
                continue
            # --- optional payment {1}{R/G} ---
            if wtype == "OptionalCostChoice":
                want_pay = (ST["phase"] == "B")
                ch = pick_optional_cost_choice(chs, want_pay)
                if ch is None:
                    wire("optional_cost_no_choice",
                         {"who": c.name, "iid": iid, "phase": ST["phase"],
                          "waiting_for": wf_of(state),
                          "choices": [
                              {"id": c2.get("id"),
                               "codes": action_codes(c2),
                               "values": value_pairs(c2)}
                              for c2 in chs]})
                    say(f"[{c.name}] OptionalCostChoice: no "
                        f"{'pay' if want_pay else 'decline'} choice "
                        f"identified; deferring")
                    continue
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch["id"]}}})
                PROMPT_DONE.add(iid)
                if ST["phase"] == "A":
                    ST["A_declined"] = True
                    ST["A_payment_offered"] = True
                    ST["payment_path_A"] = "explicit_decline"
                    say(f"[{c.name}] OptionalCostChoice phase A: DECLINE "
                        f"payment (cannot pay, 0 mana)")
                else:
                    ST["B_paid"] = True
                    ST["B_payment_offered"] = True
                    ST["payment_path_B"] = "explicit_pay"
                    say(f"[{c.name}] OptionalCostChoice phase B: PAY "
                        f"{{1}}{{R/G}}")
                acted_flag = True
                continue
            # --- fight target selection ---
            if wtype in ("TargetSelection", "TriggerTargetSelection"):
                spec = spec_type_of(opp)
                if rtype != "schema" or spec != "sequence":
                    wire("target_not_sequence",
                         {"who": c.name, "iid": iid, "rtype": rtype,
                          "spec": spec})
                    continue
                bears = set(bf_creatures(state, 1, BEARS))
                pick = None
                for ch in chs:
                    if ref_of(ch) in bears:
                        pick = ch
                        break
                if pick is None:
                    say(f"[{c.name}] target prompt: no Bears candidate; "
                        f"deferring")
                    wire("target_no_bears",
                         {"who": c.name, "iid": iid,
                          "candidates": [ch.get("id") for ch in chs]})
                    continue
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": "sequence",
                                     "data": {"choiceIds": [pick["id"]]}}})
                PROMPT_DONE.add(iid)
                ST["B_target_chosen"] = ref_of(pick)
                say(f"[{c.name}] fight target: Bears oid={ref_of(pick)}")
                acted_flag = True
                continue
            # --- legend rule (12x Slinza density) ---
            if wtype == "ChooseLegend" and "chooseLegend" in str(
                    [action_codes(ch) for ch in chs]):
                adv = find_action(merged_actions(st), "ChooseLegend")
                if adv:
                    await submit_as_is(c, adv)
                    PROMPT_DONE.add(iid)
                    say(f"[{c.name}] ChooseLegend: submitted advertised "
                        f"action as-is (keeps first)")
                    acted_flag = True
                continue
        return acted_flag

    async def common_prio(c, pid, st, acts, state):
        wtype = wf_of(state).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(f"{c.name}_keep"):
            hn = [oname(get_obj(state, o)) for o in hand_oids(state, pid)]
            lands = sum(1 for n in hn if n == FOREST)
            mulls = kept.get(f"{c.name}_mulls", 0)
            if lands >= 2 or mulls >= 3:
                kept[f"{c.name}_keep"] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{c.name} keeps (lands={lands}, mulls={mulls})")
            else:
                kept[f"{c.name}_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{c.name} mulligans #{mulls + 1} (lands={lands})")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{c.name}_bottomed"):
                pending = ((wf_of(state).get("data") or {}).get("pending", []))
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand = hand_oids(state, pid)

                def bkey(oid):
                    nm = oname(get_obj(state, oid))
                    return 0 if nm == FOREST else (
                        2 if nm in (SLINZA, BALOTH) else 1)
                picks = sorted(hand, key=bkey)[:count]
                kept[f"{c.name}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"{c.name} bottoms {count}")
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return True
        if await handle_vi(c, pid, st, state):
            return True
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
            return True
        return False

    def stack_trigger_entries(state):
        out = []
        for e in state.get("stack", []) or []:
            blob = json.dumps(e, default=str).lower()
            if "slinza" in blob and ("trigger" in blob or "ability" in blob):
                out.append(e)
        return out

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json")
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            return False

    def load_env(tag):
        p = f"{EVDIR}/{tag}.json"
        if not os.path.exists(p):
            return None
        return json.loads(open(p).read())

    async def p0_tick(st, acts, state):
        if await common_prio(p0, 0, st, acts, state):
            return True
        wtype = wf_of(state).get("type")
        # trigger phase-transition detection
        trigs = stack_trigger_entries(state)
        slinza_bf = bool(bf_creatures(state, 0, SLINZA))
        if ST["phase"] == "A" and slinza_bf and trigs \
                and not ST["A_trigger_seen"]:
            ST["A_trigger_seen"] = True
            await export("pre_A")
            notes.append(f"A trigger seen: {len(trigs)} slinza trigger "
                         f"stack entries")
            wire("phase_A_trigger", {"stack": trigs})
        if ST["phase"] == "A" and ST["A_trigger_seen"] and not ST["A_done"]:
            # cannot-pay branch completes when the trigger leaves the stack
            # with no fight. The engine may offer an explicit decline
            # (A_declined set in handle_vi) or auto-skip the unaffordable
            # payment (v0.78 run saw no prompt at 0 mana) - either way the
            # acceptance criterion is "resolution continues".
            if not trigs and not (state.get("stack") or []):
                if not ST["A_payment_offered"]:
                    ST["A_auto_skipped"] = True
                    ST["payment_path_A"] = "auto_skip"
                ST["A_done"] = True
                ST["phase"] = "B"
                await export("post_A")
                say("PHASE A complete (cannot-pay branch resolved); "
                    "moving to B")
                return True
        if ST["phase"] == "B" and slinza_bf and trigs \
                and not ST["B_trigger_seen"]:
            ST["B_trigger_seen"] = True
            await export("pre_B")
            wire("phase_B_trigger", {"stack": trigs})
        if ST["phase"] == "B" and ST["B_target_chosen"] and not ST["B_done"]:
            bears_bf = bf_creatures(state, 1, BEARS)
            bears_gy = [oname(get_obj(state, o))
                        for o in player_of(state, 1).get("graveyard", [])]
            if BEARS in bears_gy and not (state.get("stack") or []):
                ST["B_fight_resolved"] = True
                ST["B_done"] = True
                await export("post_B")
                say("PHASE B complete (fight resolved, Bears dead); finishing")
                return True
        # #4509: gate default PassPriority on my_priority
        if not (wtype == "Priority" and state.get("priority_player") == 0):
            return False
        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            n_untapped = len(untapped_forests(state, 0))
            if ST["phase"] == "A" and not bf_creatures(state, 0, SLINZA):
                for a in acts:
                    if a["type"] == "PlayLand":
                        await submit_as_is(p0, a)
                        return True
                if n_untapped == 5:
                    for a in acts:
                        d = a.get("data", {})
                        oid = d.get("object_id")
                        if a["type"] == "CastSpell" and oid is not None \
                                and oname(get_obj(state, oid)) == SLINZA:
                            say(f"P0 casts Slinza with {n_untapped} untapped "
                                f"(will leave 0 for the cannot-pay branch)")
                            wire("cast_slinza", a)
                            await submit_as_is(p0, a)
                            return True
            if ST["phase"] == "B" and slinza_bf and not ST["B_trigger_seen"]:
                for a in acts:
                    if a["type"] == "PlayLand":
                        await submit_as_is(p0, a)
                        return True
                # Baloth {G}{G}{G} reduced by Slinza's {2} to {G}; keep >=3
                if n_untapped >= 3:
                    for a in acts:
                        d = a.get("data", {})
                        oid = d.get("object_id")
                        if a["type"] == "CastSpell" and oid is not None \
                                and oname(get_obj(state, oid)) == BALOTH:
                            say(f"P0 casts Baloth with {n_untapped} untapped "
                                f"(keep >=3 for the pay branch)")
                            wire("cast_baloth", a)
                            await submit_as_is(p0, a)
                            return True
        for a in acts:
            if a["type"] == "PlayLand" and own_main:
                await submit_as_is(p0, a)
                return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        if await common_prio(p1, 1, st, acts, state):
            return True
        wtype = wf_of(state).get("type")
        if not (wtype == "Priority" and state.get("priority_player") == 1):
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    return True
            if len(untapped_forests(state, 1)) >= 2:
                for a in acts:
                    d = a.get("data", {})
                    oid = d.get("object_id")
                    if a["type"] == "CastSpell" and oid is not None \
                            and oname(get_obj(state, oid)) == BEARS:
                        say("P1 casts Grizzly Bears")
                        await submit_as_is(p1, a)
                        return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        preA = load_env("pre_A")
        postA = load_env("post_A")
        preB = load_env("pre_B")
        postB = load_env("post_B")
        nrej = ST.get("rejections", 0)
        notes.append(f"A_trigger_seen={ST['A_trigger_seen']} "
                     f"A_payment_offered={ST['A_payment_offered']} "
                     f"A_declined={ST['A_declined']} "
                     f"A_auto_skipped={ST['A_auto_skipped']} "
                     f"B_trigger_seen={ST['B_trigger_seen']} "
                     f"B_paid={ST['B_paid']} "
                     f"B_target_chosen={ST['B_target_chosen']} "
                     f"B_fight_resolved={ST['B_fight_resolved']} "
                     f"rejections={nrej}")
        if preA is not None:
            s = preA["state"]
            slinza_bf = bool(bf_creatures(s, 0, SLINZA))
            trigs = stack_trigger_entries(s)
            untapped = len(untapped_forests(s, 0))
            ok = slinza_bf and len(trigs) >= 1 and untapped == 0
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: slinza_on_bf={slinza_bf}, "
                         f"trigger_stack_entries={len(trigs)}, "
                         f"untapped_forests={untapped}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre_A.json missing (Slinza trigger never seen)")
        if preA is not None and postA is not None:
            s = postA["state"]
            slinza = bf_creatures(s, 0, SLINZA)
            dmg = damage_on(s, slinza[0]) if slinza else None
            gy1 = [oname(get_obj(s, o))
                   for o in player_of(s, 1).get("graveyard", [])]
            stack_empty = not (s.get("stack") or [])
            wf = wf_of(s).get("type")
            ok = (dmg == 0 and stack_empty and wf == "Priority"
                  and ST["rejections"] == 0
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20
                  and not ST["stuck_watch_fired"])
            ass["A2_cannot_pay"] = "passed" if ok else "failed"
            notes.append(f"A2: payment_path={ST['payment_path_A']}, "
                         f"slinza_damage={dmg}, P1_gy={gy1}, "
                         f"stack_empty={stack_empty}, wf={wf}, "
                         f"rejections={ST['rejections']}, "
                         f"life={life_of(s, 0)}/{life_of(s, 1)}")
        else:
            ass["A2_cannot_pay"] = "failed"
            notes.append("A2: missing pre/post A states")
        if preB is not None and postB is not None:
            s = postB["state"]
            s0 = preB["state"]
            slinza = bf_creatures(s, 0, SLINZA)
            dmg = damage_on(s, slinza[0]) if slinza else None
            gy1 = [oname(get_obj(s, o))
                   for o in player_of(s, 1).get("graveyard", [])]
            stack_empty = not (s.get("stack") or [])
            tapped_delta = tapped_forests(s, 0) - tapped_forests(s0, 0)
            ok = (ST["B_target_chosen"] is not None and BEARS in gy1
                  and dmg == 2 and stack_empty
                  and life_of(s, 0) == 20 and life_of(s, 1) == 20
                  and tapped_delta >= 2)
            ass["A3_can_pay"] = "passed" if ok else "failed"
            notes.append(f"A3: payment_path={ST['payment_path_B']}, "
                         f"target_chosen={ST['B_target_chosen']}, "
                         f"bears_in_gy={BEARS in gy1}, slinza_damage={dmg} "
                         f"(want 2), tapped_forest_delta={tapped_delta} "
                         f"(want >=2), stack_empty={stack_empty}, "
                         f"life={life_of(s, 0)}/{life_of(s, 1)}")
        else:
            ass["A3_can_pay"] = "failed"
            notes.append("A3: missing pre/post B states")
        final = postB if postB is not None else postA
        if final is not None:
            s = final["state"]
            wf = wf_of(s).get("type")
            ok = (not (s.get("stack") or []) and wf == "Priority"
                  and not ST["stuck_watch_fired"])
            ass["A4_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A4: stack_empty={not (s.get('stack') or [])}, "
                         f"wf={wf}, stuck_watch={ST['stuck_watch_fired']}")
        else:
            ass["A4_cleanup"] = "failed"
            notes.append("A4: no final post state")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("setup never reached a Slinza trigger")
        elif all(ass[k] == "passed" for k in
                 ("A1_setup_ok", "A2_cannot_pay", "A3_can_pay", "A4_cleanup")):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("the Slinza payment-decision path did not behave "
                         "per the triage acceptance criteria on this build")
        return verdict

    async def finish():
        dur = time.time() - t_start
        for tag in ("post_A", "post_B"):
            if not os.path.exists(f"{EVDIR}/{tag}.json"):
                try:
                    await export(tag)
                except Exception:
                    pass
        verdict = evaluate()
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9375,
            "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5936_085.py",
                     "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in ST.items()},
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x spell density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "Phase A exercises the cannot-pay branch with zero available "
                "mana (all Forests tapped casting Slinza); the original "
                "report's exact mana position is unknown (Discord images expired).",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Slinza, the Spiked Stampede + 12x Leatherback "
                          "Baloth + 24x Forest; P1: 12x Grizzly Bears + 36x Forest",
            "contract_line": "Slinza optional enters trigger: (A) payment cannot "
                             "be paid (0 mana): decline/auto-skip must resolve "
                             "with no fight and no stall; (B) payment accepted "
                             "with mana available must produce the conditional "
                             "fight vs the chosen opposing creature",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        # scenario source copy (for evidence self-containment)
        with open(f"{EVDIR}/scenario_5936_085.py", "w") as f:
            f.write(open(f"{BACKFILL}/driver/scenario_5936_085.py").read())
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        # summary PNG from saved states/assertions (after all say() done)
        subprocess.run([sys.executable,
                        f"{BACKFILL}/driver/render_summary_5936.py",
                        EVDIR, str(ISSUE),
                        "Slinza, the Spiked Stampede - optional payment"],
                       check=True)
        # manifest last (#6916: after all file writes)
        files = sorted(f for f in os.listdir(EVDIR)
                       if f not in ("manifest.sha256",))
        with open(f"{EVDIR}/manifest.sha256", "w") as mf:
            for fn in files:
                h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
                mf.write(f"{h}  {fn}\n")
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, pid, tick in ((p0, 0, p0_tick), (p1, 1, p1_tick)):
            drain_rejections(c)
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
            state = st.get("state", {})
            wtype = wf_of(state).get("type")
            mid_trigger = (ST["A_trigger_seen"] and not ST["A_done"]) or \
                          (ST["B_trigger_seen"] and not ST["B_done"])
            if mid_trigger or (wtype and wtype != "Priority"):
                log_vi(c, st, "trig" if mid_trigger else f"wf-{wtype}")
        if ST["B_done"]:
            say("both phases complete; finishing")
            await finish()
            return
        if p0.latest and p0.latest["state"].get("turn_number", 0) >= 20 \
                and not ST["B_done"]:
            notes.append("turn 20 reached without completing both phases; "
                         "bailing out to evaluation")
            say("turn 20 bail-out; finishing")
            await finish()
            return
        mid_trigger = (ST["A_trigger_seen"] and not ST["A_done"]) or \
                      (ST["B_trigger_seen"] and not ST["B_done"])
        if mid_trigger and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not mid_trigger:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            wf = wf_of(s).get("type")
            ST["stuck_watch_fired"] = True
            notes.append(f"STUCK WATCH FIRED (180s mid-trigger): waiting_for={wf} "
                         f"priority_player={s.get('priority_player')}")
            say(f"STUCK WATCH FIRED: waiting_for={wf}")
            try:
                await export("mid_stall")
            except Exception:
                pass
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={wf_of(s).get('type')} "
                f"pp={s.get('priority_player')} "
                f"slinza_bf={bool(bf_creatures(s, 0, SLINZA))} "
                f"untapped_forests={len(untapped_forests(s, 0))} "
                f"phase={ST['phase']} A_done={ST['A_done']} B_done={ST['B_done']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
