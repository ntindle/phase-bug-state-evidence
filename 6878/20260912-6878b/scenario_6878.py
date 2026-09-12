#!/usr/bin/env python3
"""Issue #6878: Duskwatch Recruiter - can't choose the order of the cards put
on the bottom of the library.

Oracle: "{2}{G}: Look at the top three cards of your library. You may reveal
a creature card from among them and put it into your hand. Put the rest on
the bottom of your library in any order."
Reported: the controller cannot choose the order of the rest.
Second symptom (matthewevans 2026-09-04 comment): the ability reveals all
three looked-at cards to the opponent instead of only the creature put
into hand.

Plan (native engine, v0.80.0 / protocol 69, two human-client seats):
  P0: 12x Duskwatch Recruiter + 4x Llanowar Elves + 4x Grizzly Bears
      + 20x Forest (activation {2}{G}; engine auto-taps mana).
  P1: 12x Llanowar Elves + 28x Forest; casts a 1-mana creature on each
      of its main phases so "a spell was cast last turn" stays true at
      every P0 upkeep, suppressing the Recruiter's transform trigger.
  Round 1 (accept branch): cast Recruiter (needs 6+ untapped Forests),
  next P0 main phase cast a safety creature spell (suppresses the upkeep
  transform trigger), then activate. Keep the first creature of the top 3.
  The engine's own waiting_for data is expected to carry
  rest_order:'preserve' and offer NO ordering choice for the rest.
  Round 2 (decline branch, control): on a later P0 main phase (safety
  spell cast first), activate again and answer the keep select with zero
  cards. Expect an ordering choice for all three; observe whether one
  appears.

Behavioral contract:
  A1 setup_ok          Recruiter on P0 BF, activation paid, ability resolved
  A2 keep_offered      round-1 Dig select offered the 3 looked-at cards
                       (keep_count 1, up_to)
  A3 order_offered_r1  an ordering choice for the rest was offered (round 1)
  A4 rest_preserved_r1 the rest sit at the bottom of the library in the
                       original relative order (fixed order, not chosen)
  A5 no_opponent_leak  P1's view during Dig shows no names of the
                       looked-at cards
  A6 decline_branch_r2 decline (0 kept) still offers no ordering choice;
                       all three land on the bottom in preserved order
                       (not-run if the transform spoils round 2)
  A7 cleanup           stack empty, no stuck decision, game proceeds

Verdict: reproduced iff A1+A2 pass and (A3 fails or A6 fails).
not-reproduced iff A1..A7 all pass (A6 may be not-run only if the
transform genuinely spoils round 2). blocked otherwise.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6878b"
ISSUE = "6878"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RECRUITER = "Duskwatch Recruiter"
HOWLER = "Krallenhorde Howler"
ELVES = "Llanowar Elves"
BEARS = "Grizzly Bears"
FOREST = "Forest"
MTN = "Mountain"
P0_DECK = [(RECRUITER, 12), (ELVES, 4), (BEARS, 4), (FOREST, 20)]
# P1 casts a 1-mana creature on each of its main phases so that "a spell
# was cast last turn" stays true at every P0 upkeep; this suppresses the
# Duskwatch Recruiter upkeep transform and keeps the Recruiter on the
# battlefield for round 2 of the scenario.
P1_DECK = [(ELVES, 12), (FOREST, 28)]
CREATURES = {RECRUITER, ELVES, BEARS}

TIMEOUT = 1500
DIG_WATCHDOG = 150

ST = {}
OBS = {}
SUBMITTED = set()
MULLS = {}
SHAPES = set()
WF_SEEN = []
C0 = None


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
    except Exception:
        pass


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def perm_oids(state, pid, name):
    return [str(oid) for oid, o in bf(state, pid) if oname(o) == name]


def untapped_forests(state, pid):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == FOREST and not o.get("tapped")]


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST.get("stage")})


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST.get("stage")})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def library_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("library") or []
    return []


def top3_of(state, pid):
    lib = library_of(state, pid)
    out = []
    for oid in lib[:3]:
        o = state["objects"].get(str(oid), {})
        out.append((str(oid), oname(o)))
    return out


def find_vi_choice(st, code, source_ref=None):
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []):
        resp = op.get("response", {})
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp["data"].get("choices", []):
            surfs = ch.get("surfaces", [])
            codes = [s.get("data", {}).get("code") for s in surfs]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [s.get("data", {}).get("reference") for s in surfs
                        if s.get("data", {}).get("role") == "source"]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
    return None


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        codes = []
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if s.get("type") == "action":
                    codes.append(d.get("code") or "")
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "action_codes": codes, "text": ch.get("text"),
                    "label": (ch.get("label") or ch.get("name"))})
    return out


def build_choose(cid):
    return {"type": "choose", "data": {"choiceId": cid}}


def build_schema(resp, cids):
    rtype = resp.get("type")
    spec = (resp.get("data") or {}).get("spec") or {}
    stype = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and stype in ("sequence", "select"):
        return {"type": stype, "data": {"choiceIds": cids}}
    return None


def dig_ctx():
    """Per-round dig context: 'r1' or 'r2'."""
    return "r2" if ST.get("stage") == "DIG2" else "r1"


async def handle_dig(c, state, acts, st):
    """Drive the Dig decisions for P0 in DIG1/DIG2. Returns True if acted."""
    stage = ST.get("stage")
    if stage not in ("DIG1", "DIG2"):
        return False
    if (wf_data(state) or {}).get("player") != 0:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    ctx = dig_ctx()
    K = OBS.setdefault(ctx, {})
    acted = False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if not iid or iid in SUBMITTED:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cands = candidate_info(opp, state)
        shape = (stage, wf_type(state), rtype,
                 tuple(sorted(str(x.get("choice_id")) for x in cands)))
        if shape not in SHAPES:
            SHAPES.add(shape)
            wire("dig_prompt", {"round": ctx,
                                "waiting_for": wf_data(state),
                                "response_type": rtype,
                                "spec": (resp.get("data") or {}).get("spec"),
                                "candidates": cands,
                                "opportunity": opp})
            say(f"[P0] {ctx} Dig prompt wf={wf_type(state)} rtype={rtype} "
                f"ncand={len(cands)}")
            for x in cands:
                say(f"    cand {x['choice_id']} ref={x['ref']} "
                    f"name={x['name']} zone={x['zone']} codes={x['action_codes']}")

        card_cands = [x for x in cands if x.get("ref")]
        looked = set(K.get("looked_oids") or [])
        # Phase 1: the optional keep choice among the looked-at cards.
        if not K.get("keep_decided"):
            lib_refs = [x for x in card_cands
                        if x.get("zone") == "Library" and x["ref"] in looked]
            if lib_refs and rtype == "schema":
                K["keep_prompt_seen"] = True
                K["keep_spec"] = (resp.get("data") or {}).get("spec")
                K["keep_wf"] = wf_data(state)
                if ctx == "r1":
                    # accept branch: first creature in top-3 order
                    want = None
                    for oid, nm in K["top3"]:
                        if nm in CREATURES:
                            want = next((x for x in lib_refs
                                         if x["ref"] == oid), None)
                            if want:
                                break
                    cids = [want["choice_id"]] if want else []
                else:
                    # decline branch: keep zero cards
                    cids = []
                rout = build_schema(resp, cids)
                if rout is not None:
                    await send_interaction(
                        c, {"interactionId": iid, "response": rout})
                    SUBMITTED.add(iid)
                    K["keep_decided"] = True
                    K["kept_cids"] = cids
                    kept_refs = [x["ref"] for x in lib_refs
                                 if x["choice_id"] in cids]
                    K["kept_refs"] = kept_refs
                    K["rest_oids"] = [oid for oid, _nm in K["top3"]
                                      if oid not in kept_refs]
                    say(f"[P0] {ctx} Dig keep answered cids={cids} "
                        f"kept={kept_refs} rest={K['rest_oids']}")
                    acted = True
                    continue
            # not the keep prompt (e.g. a mana/priority menu): leave it
            # to the generic handlers below; never misclassify.
            continue

        # Phase 2: a genuine bottom-order prompt references the rest
        # oids (Library zone) in a schema opportunity.
        if K.get("keep_decided") and not K.get("order_decided"):
            rest_refs = [x for x in card_cands
                         if x.get("zone") == "Library"
                         and x["ref"] in set(K.get("rest_oids") or [])]
            if rest_refs and rtype == "schema":
                want_ids = []
                for oid in reversed(K["rest_oids"]):
                    hit = next((x for x in rest_refs if x["ref"] == oid),
                               None)
                    if hit:
                        want_ids.append(hit["choice_id"])
                rout = build_schema(resp, want_ids)
                if rout and want_ids:
                    K["order_offered"] = True
                    K["order_submitted"] = [
                        next(x["ref"] for x in rest_refs
                             if x["choice_id"] == cid) for cid in want_ids]
                    wire("order_prompt_answered",
                         {"round": ctx,
                          "submitted_refs": K["order_submitted"]})
                    await send_interaction(
                        c, {"interactionId": iid, "response": rout})
                    SUBMITTED.add(iid)
                    K["order_decided"] = True
                    say(f"[P0] {ctx} bottom-order answered: "
                        f"{K['order_submitted']}")
                    acted = True
                    continue
    return acted

async def maybe_safety_cast(c, state, acts, pid):
    """Cast a cheap creature spell on P0's turn to keep the upkeep
    transform trigger suppressed (controller must have cast a spell on
    its previous turn). Returns True if it cast."""
    if OBS.get("spell_cast_turn", -1) == state.get("turn_number"):
        return False
    for nm in (BEARS, ELVES, RECRUITER):
        chand = find_hand(state, pid, nm)
        if not chand:
            continue
        # don't burn the last Recruiter while none is on the battlefield
        if nm == RECRUITER and not perm_oids(state, pid, RECRUITER) \
                and sum(1 for o in hand_oids(state, pid)
                        if oname(state["objects"][o]) == RECRUITER) <= 1:
            continue
        for a in acts:
            if a["type"] == "CastSpell" and str(
                    a.get("data", {}).get("object_id")) == chand:
                await submit_as_is(c, a)
                OBS["spell_cast_turn"] = state.get("turn_number")
                say(f"[P0] safety cast {nm} (turn {state.get('turn_number')})")
                return True
    return False


async def tick(c, pid, is_p0, p0c, p1c):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])

    # --- mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            names = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
            keep_ok = (RECRUITER in names and FOREST in names) \
                if is_p0 else (FOREST in names)
            choice = "Keep" if (keep_ok or MULLS[c.name] >= 2) else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            pending = wf_data(state).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            keep = {RECRUITER, FOREST, BEARS, ELVES} \
                if is_p0 else {FOREST, ELVES}
            pref = [o for o in h if oname(state["objects"][o]) not in keep]
            pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True

    # --- empty combat declarations
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = dict(a)
            sub["data"] = dict(a.get("data", {}))
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True

    # --- P0 Dig decisions take precedence; never pass during them
    if is_p0:
        if await handle_dig(c, state, acts, st):
            return True
        if wf_type(state) not in (None, "Priority") \
                and (wf_data(state) or {}).get("player") == 0 \
                and ST.get("stage") in ("DIG1", "DIG2"):
            return False

    # --- mana producers: submit advertised payments as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True

    # --- P0 main-phase plan
    if is_p0 and is_my_main(state, pid):
        stage = ST.get("stage")
        rec_bf = perm_oids(state, pid, RECRUITER)
        howler_bf = perm_oids(state, pid, HOWLER)
        if stage == "SETUP":
            lid = find_hand(state, pid, FOREST)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True
            if not rec_bf:
                rh = find_hand(state, pid, RECRUITER)
                if rh and len(untapped_forests(state, pid)) >= 6:
                    for a in acts:
                        if a["type"] == "CastSpell" and str(
                                a.get("data", {}).get("object_id")) == rh:
                            await submit_as_is(c, a)
                            OBS["spell_cast_turn"] = state.get("turn_number")
                            OBS["recruiter_cast_turn"] = \
                                state.get("turn_number")
                            say("[P0] casts Duskwatch Recruiter "
                                f"(turn {state.get('turn_number')})")
                            return True
        elif stage in ("R1ARM", "R2ARM"):
            if howler_bf and not rec_bf:
                say("[P0] Recruiter transformed to Krallenhorde Howler; "
                    f"stage={stage}")
                wire("transformed", {"turn": state.get("turn_number"),
                                     "stage": stage})
                await export_now("spoiled_transform.json")
                OBS["spoiled_stage"] = stage
                ST["stop"] = True
                return False
            if rec_bf:
                rec_oid = rec_bf[0]
                # safety spell first (suppresses the upkeep transform)
                if await maybe_safety_cast(c, state, acts, pid):
                    return True
                want_mana = 3
                if not state.get("stack") and len(
                        untapped_forests(state, pid)) >= want_mana:
                    f = find_vi_choice(st, "activateAbility",
                                       source_ref=rec_oid)
                    if f:
                        iid, ch = f
                        ctx = "r1" if stage == "R1ARM" else "r2"
                        K = OBS.setdefault(ctx, {})
                        s = await export_now(
                            f"pre_{ctx}.json")
                        env = json.loads(s)
                        stt = env["state"]
                        K["top3"] = top3_of(stt, 0)
                        K["looked_oids"] = [oid for oid, _ in K["top3"]]
                        wire("top3", {"round": ctx, "top3": K["top3"]})
                        say(f"[P0] {ctx} top3: {K['top3']}")
                        await send_interaction(
                            c, {"interactionId": iid,
                                "response": build_choose(ch["id"])})
                        SUBMITTED.add(iid)
                        K["activated"] = True
                        K["activated_turn"] = state.get("turn_number")
                        ST["stage"] = "DIG1" if stage == "R1ARM" else "DIG2"
                        K["dig_t0"] = time.time()
                        say(f"[P0] {ctx} activated Duskwatch Recruiter "
                            f"(choice {ch['id']})")
                        return True
                    if not OBS.get("armed_diag"):
                        OBS["armed_diag"] = True
                        say(f"[P0] {stage} diag: turn={state.get('turn_number')} "
                            f"untapped_forests="
                            f"{len(untapped_forests(state, pid))} "
                            f"stack={len(state.get('stack') or [])}")
            lid = find_hand(state, pid, FOREST)
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            a.get("data", {}).get("object_id")) == lid:
                        await submit_as_is(c, a)
                        return True

    # --- P1: cast a 1-mana creature each main phase (keeps "a spell was
    # cast last turn" true at every P0 upkeep so the Recruiter never
    # transforms), then land drops
    if not is_p0 and is_my_main(state, pid):
        if OBS.get("p1_spell_cast_turn", -1) != state.get("turn_number"):
            chand = find_hand(state, pid, ELVES)
            if chand:
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id")) == chand:
                        await submit_as_is(c, a)
                        OBS["p1_spell_cast_turn"] = \
                            state.get("turn_number")
                        say(f"[P1] casts Llanowar Elves "
                            f"(turn {state.get('turn_number')})")
                        return True
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True

    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def bottom_segment(lib, oids):
    """Positions of oids in lib (index 0 = top)."""
    pos = {}
    for i, o in enumerate(lib):
        if str(o) in oids:
            pos[str(o)] = i
    return pos


async def attempt():
    ST.update({"stage": "SETUP"})
    OBS.update({"rejections": [], "p1_dig_view": None,
                "dig_view_captured": False})
    MULLS.update({"P0": 0, "P1": 0})

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0_deck": P0_DECK,
                  "p1_deck": P1_DECK})

    global C0
    C0 = p0
    t0 = time.time()

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    while time.time() - t0 < TIMEOUT and not ST.get("stop"):
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej:
                OBS["rejections"].extend(
                    {"at": now, "who": c.name,
                     "type": r["type"], "data": r["data"]} for r in rej)
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0, p0, p1):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if wf_type(state) == "GameOver":
            OBS["notes"] = OBS.get("notes", []) + \
                ["game over before sequence completed"]
            say("game over -> stopping")
            ST["stop"] = True
            break

        # SETUP -> R1ARM: recruiter on BF, P0 main phase, stack empty
        if ST["stage"] == "SETUP" and is_my_main(state, 0) \
                and not state.get("stack") and perm_oids(state, 0, RECRUITER):
            say(f"=== stage -> R1ARM (turn {state.get('turn_number')}) ===")
            ST["stage"] = "R1ARM"

        # capture P1's view during the round-1 Dig decision
        if ST.get("stage") == "DIG1" and not OBS["dig_view_captured"]:
            if (wf_data(state) or {}).get("player") == 0 and p1.latest:
                p1s = p1.latest["state"]
                snap = {}
                for oid, nm in (OBS.get("r1", {}).get("top3") or []):
                    o = p1s["objects"].get(str(oid), {})
                    snap[oid] = {"name": oname(o), "zone": o.get("zone"),
                                 "face_down": o.get("face_down"),
                                 "revealed": o.get("revealed")}
                OBS["p1_dig_view"] = snap
                OBS["dig_view_captured"] = True
                wire("p1_dig_view", snap)
                say(f"[P1 view during Dig] {snap}")

        # Dig watchdog
        stage = ST.get("stage")
        if stage in ("DIG1", "DIG2"):
            ctx = "r1" if stage == "DIG1" else "r2"
            K = OBS.get(ctx, {})
            if K.get("dig_t0") and now - K["dig_t0"] > DIG_WATCHDOG \
                    and not K.get("resolved"):
                say(f"{ctx} DIG watchdog fired")
                wire("dig_watchdog", {"round": ctx,
                                      "waiting_for": wf_data(state)})
                await export_now(f"mid_dig_{ctx}.json")
                OBS["notes"] = OBS.get("notes", []) + \
                    [f"{ctx} dig watchdog fired; see mid_dig_{ctx}.json"]
                ST["stop"] = True
                break

        # resolution complete: keep decided, stack empty, no P0 decision
        if stage in ("DIG1", "DIG2"):
            ctx = "r1" if stage == "DIG1" else "r2"
            K = OBS.get(ctx, {})
            if K.get("keep_decided") and not state.get("stack") \
                    and not K.get("resolved"):
                if (wf_data(state) or {}).get("player") != 0:
                    K["resolved"] = True
                    s = await export_now(f"post_{ctx}.json")
                    env = json.loads(s)
                    stt = env["state"]
                    lib = [str(o) for o in library_of(stt, 0)]
                    K["post_bottom"] = lib[-6:]
                    K["rest_positions"] = bottom_segment(
                        lib, K.get("rest_oids") or [])
                    # kept card should be in P0 hand
                    K["kept_in_hand"] = [
                        r for r in (K.get("kept_refs") or [])
                        if stt["objects"].get(str(r), {}).get("zone")
                        == "Hand"]
                    say(f"=== {ctx} resolved; post exported; "
                        f"rest_positions={K['rest_positions']} ===")
                    if ctx == "r1":
                        ST["stage"] = "R2ARM"
                        say("=== stage -> R2ARM ===")
                    else:
                        ST["stop"] = True
                    continue

    say(f"loop ended: stage={ST.get('stage')} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": ST.get("stage"),
                      "obs_keys": list(OBS.keys())})

    # ---- assertions
    A = {}
    r1 = OBS.get("r1", {})
    r2 = OBS.get("r2", {})

    A["A1_setup_ok"] = (
        "passed" if (r1.get("activated") and r1.get("resolved")
                     and not OBS.get("rejections")) else "failed")
    keep_wf = r1.get("keep_wf") or {}
    A["A2_keep_offered"] = (
        "passed" if (r1.get("keep_prompt_seen")
                     and keep_wf.get("keep_count") == 1
                     and keep_wf.get("up_to") is True
                     and sorted(map(int, keep_wf.get("cards") or []))
                     == sorted(map(int, r1.get("looked_oids") or [])))
        else "failed")
    A["A3_order_offered_r1"] = (
        "passed" if r1.get("order_offered") else "failed")
    # rest preserved: rest oids occupy bottom slots in original relative order
    # (index 0 = top of library; bottom = highest indices)
    rest = r1.get("rest_oids") or []
    pos = r1.get("rest_positions") or {}
    preserved = (len(rest) > 0 and len(pos) == len(rest)
                 and [pos[o] for o in rest]
                 == sorted(pos[o] for o in rest))
    A["A4_rest_preserved_r1"] = "passed" if preserved else "failed"
    A["A4_rest_preserved_r1_detail"] = {
        "rest_oids": rest, "positions": pos,
        "post_bottom_tail": r1.get("post_bottom")}
    p1v = OBS.get("p1_dig_view") or {}
    leaked = [oid for oid, v in p1v.items()
              if v.get("name") not in (None, "", "Hidden Card")]
    A["A5_no_opponent_leak"] = (
        "passed" if (OBS.get("dig_view_captured") and not leaked)
        else "failed")
    A["A5_no_opponent_leak_detail"] = {"view": p1v, "leaked": leaked}
    if r2.get("activated") and r2.get("resolved"):
        A["A6_decline_branch_r2"] = (
            "failed" if not r2.get("order_offered") else "passed")
        A["A6_decline_branch_r2_detail"] = {
            "kept": r2.get("kept_refs"), "rest_oids": r2.get("rest_oids"),
            "rest_positions": r2.get("rest_positions"),
            "order_offered": bool(r2.get("order_offered"))}
    elif OBS.get("spoiled_stage") == "R2ARM":
        A["A6_decline_branch_r2"] = "not-run"
        A["A6_decline_branch_r2_detail"] = \
            "transform spoiled round 2 (see spoiled_transform.json)"
    else:
        A["A6_decline_branch_r2"] = "not-run"
        A["A6_decline_branch_r2_detail"] = \
            "round 2 did not complete (see notes)"
    notes = OBS.get("notes", [])
    watchdog_fired = any("watchdog" in n for n in notes)
    A["A7_cleanup"] = (
        "passed" if (r1.get("resolved") and not watchdog_fired
                     and wf_type(state) != "GameOver")
        else "failed")

    verdict = "blocked"
    core = [A["A1_setup_ok"], A["A2_keep_offered"]]
    if all(v == "passed" for v in core):
        if A["A3_order_offered_r1"] == "failed" \
                or A["A6_decline_branch_r2"] == "failed":
            verdict = "reproduced"
        elif A["A3_order_offered_r1"] == "passed" \
                and A["A6_decline_branch_r2"] in ("passed", "not-run") \
                and all(v in ("passed", "not-run") for v in A.values()):
            verdict = "not-reproduced"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": int(ISSUE), "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"observations": OBS, "waiting_for_seq": WF_SEEN,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    say(f"verdict={verdict}")
    for k, v in A.items():
        if not k.endswith("_detail"):
            say(f"  {k}: {v}")
    return verdict


async def main():
    v = await attempt()
    WIRE.close()
    RUNLOG.close()
    return v


if __name__ == "__main__":
    asyncio.run(main())
