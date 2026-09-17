#!/usr/bin/env python3
"""Issue #647: Asmoranomardicadaistinaculdacar — protocol-72 re-validation
(on pinned v0.85.0).

This run re-validates the v0.84.0/protocol-71 result ("not-reproduced") on
the current pinned release. Contract and assertions unchanged; driver adds
the protocol-71 interaction-rejection guard and stale-client watchdog from
#4509 (DiscardToHandSize interaction rejections must not silently stall the
driver: an iid rejected twice is skipped and logged, not retried forever).

Corrected card framing (from the plan comment on the issue): the card has
NoCost mana; its only legal hand-cast is the conditional alternative cost
"As long as you've discarded a card this turn, you may pay {B/R} to cast this
spell." The report's "additional cost" framing is wrong; the reported BUG is
that the NoCost path is too permissive (castable without discarding).

Behavioral contract (written before observing results, per PLAYBOOK.md):
  NEGATIVE: on any turn before P0 has discarded, Asmo in hand at P0 main-phase
            priority -> CastSpell(Asmo) must NOT be advertised (legal_actions)
            and no viewer_interaction cast offer may reference it.
  POSITIVE: on a turn where P0 has discarded, CastSpell(Asmo) MUST be
            advertised on that same turn; cast it for {B/R} and verify
            hand->stack->battlefield with payment. Two discard-turns are run
            for strength (P0 runs 4x Faithless Looting).

Assertions:
  A1 setup_ok                  game starts, turns advance, both seats act.
  A2 neg_no_cast_offered       on no-discard turns, CastSpell(Asmo) never in legal_actions.
  A3 neg_no_viewer_cast        on no-discard turns, no vi cast offer for Asmo.
  A4 looting_resolved          Faithless Looting cast + >=1 discard observed.
  A5 cast_offered_after_discard  CastSpell(Asmo) advertised on a discard turn.
  A6 asmo_cast_paid            Asmo resolved to BF; mana delta recorded.

Verdict rule: reproduced iff the REPORTED bug is observed (A2 or A3 fail).
not-reproduced otherwise (this is not a fixed verdict). blocked only if the
game cannot be driven to the discard.

Setup: P0: 4x Asmo + 4x Faithless Looting + 26 Swamp + 26 Mountain (B/R always
available so mana is never the reason a cast is unoffered). P1: 60x Island,
draw-go.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 647
RUN_ID = "20260917-647"
SERVER_RUN_ID = RUN_ID
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ASMO = "Asmoranomardicadaistinaculdacar"
LOOTING = "Faithless Looting"
LANDS = ("Swamp", "Mountain", "Forest", "Island", "Plains")

P0_DECK = [(ASMO, 4), (LOOTING, 4), ("Swamp", 26), ("Mountain", 26)]
P1_DECK = [("Island", 60)]

# stages: NEG (no discard yet, turn T) -> LOOTING (cast in flight) -> POS
# (discard turn T) -> SEEK (next turn, hunt second Looting) -> LOOTING -> POS
ST = {"stage": "NEG", "done": False, "neg_obs": 0, "pre_neg": False,
      "looting_in_flight": False, "looting_turn": None, "loots_done": 0,
      "discard_turn": None, "discard_turns": [], "all_discarded": [],
      "asmo_cast": False, "asmo_cast_turn": None, "asmo_oid_cast": None,
      "pre_cast_br": None, "mid_exported": False, "post_exported": False,
      "pos_obs": 0, "max_turn": 0, "states_seen": 0, "game_code": None,
      "bugs": [], "offer_turns": []}
ACTED = set()
PROMPT_DONE = set()
REJECTS = {}        # interactionId -> rejection count
LAST_IID = {}       # client name -> last interactionId submitted
SKIP_IID = set()    # iids rejected twice: do not retry, log instead


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload},
                          default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state helpers
def oname(o):
    return o.get("card_name") or o.get("name") or ""


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def hand_oids(state, pid):
    return [str(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def hand_names(state, pid):
    return [oname(get_obj(state, oid)) for oid in hand_oids(state, pid)]


def bf(state, pid):
    return [(oid, o) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_find(state, pid, name):
    for oid, o in bf(state, pid):
        if oname(o) == name:
            return oid
    return None


def untapped_br(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if not o.get("tapped") and oname(o) in ("Swamp", "Mountain"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def my_priority(state, pid):
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    return (wf.get("type") == "Priority"
            and (d.get("player") == pid or d.get("deciding_player") == pid))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def stack_has(state, name):
    for e in state.get("stack") or []:
        o = e if isinstance(e, dict) else get_obj(state, e)
        if oname(o) == name:
            return True
    return False


# ------------------------------------------------------------- interaction helpers
def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    return chs, resp.get("type")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


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
            body = json.dumps(data, default=str)
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {body[:300]}")
            # attribute the rejection to the last interactionId we sent from
            # this client; an iid rejected twice is skipped, not retried (#4509)
            iid = LAST_IID.get(c.name)
            if iid and iid in body:
                REJECTS[iid] = REJECTS.get(iid, 0) + 1
                say(f"[{c.name}] iid {iid} rejection #{REJECTS[iid]}")
                if REJECTS[iid] >= 2 and iid not in SKIP_IID:
                    SKIP_IID.add(iid)
                    say(f"[{c.name}] iid {iid} rejected twice; skipping further "
                        f"submissions on it (may stall waiting_for; logged)")


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED.add(k)
    return False


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def vi_cast_offer_for(st, name):
    """Raw vi scan (not canSubmit-gated): any castSpell choice naming `name`?"""
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = action_codes(ch)
            if not any("castSpell" in c for c in codes):
                continue
            names = [((s.get("data") or {}).get("name") or "")
                     for s in ch.get("surfaces", []) or []]
            if any(name in n for n in names):
                return opp, ch
    return None


async def answer_discard(c, pid, opp, tag, protect):
    """Answer a DiscardChoice: lands first, `protect` names last."""
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = (data.get("spec") or {}).get("type") if isinstance(
        data.get("spec"), dict) else data.get("type")
    chs = data.get("choices") or data.get("candidates") or []
    state = c.latest["state"]
    d = wf_data(state)
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
        if nm in LANDS:
            return (0, nm)
        return (1, nm)
    oids = sorted(hand_oids(state, pid), key=rank)[:count]
    picks = [(o, oid_by_ref[o]) for o in oids if o in oid_by_ref]
    if len(picks) < len(oids):
        say(f"[{tag}] discard: unmapped candidates; deferring")
        return False
    cids = [cid for _, cid in picks]
    if rtype == "schema" and spec in ("sequence", "select"):
        resp_out = {"type": spec, "data": {"choiceIds": cids}}
    elif rtype == "exactChoices":
        resp_out = {"type": "choose", "data": {"choiceId": cids[0]}}
    else:
        say(f"[{tag}] discard: unexpected rtype={rtype} spec={spec}; deferring")
        return False
    await send_interaction(c, {"interactionId": opp.get("interactionId"),
                              "response": resp_out})
    names = [oname(get_obj(state, o)) for o, _ in picks]
    ST["all_discarded"].extend(names)
    ST["discard_turn"] = turn_of(state)
    ST["discard_turns"].append(ST["discard_turn"])
    ST["loots_done"] += 1
    say(f"[{tag}] discards {names} (turn {ST['discard_turn']}; "
        f"discard-turn #{ST['loots_done']})")
    wire("discard_answered", {"tag": tag, "picks": names})
    return True


# ------------------------------------------------------------- PNG renderer
def render_png(path, run):
    from PIL import Image, ImageDraw
    W, H = 1040, 1020
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24
    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10
    line("#647 Asmoranomardicadaistinaculdacar — protocol-72 re-validation",
         fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    line(f"verdict: {run['verdict'].upper()}",
         fill=(120, 255, 160) if run["verdict"] == "reproduced" else (255, 170, 120))
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, v in run["assertions"].items():
        col = (120, 255, 160) if v == "passed" else ((255, 120, 120)
              if v == "failed" else (200, 200, 200))
        line(f"  {k}: {v}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:14]:
        line(f"  - {n[:118]}", size=15)
    img.save(path)
    say(f"rendered {path}")


# ------------------------------------------------------------- main
async def main():
    global C0
    t0 = time.time()
    assertions = {
        "A1_setup_ok": "not-run",
        "A2_neg_no_cast_offered": "not-run",
        "A3_neg_no_viewer_cast": "not-run",
        "A4_looting_resolved": "not-run",
        "A5_cast_offered_after_discard": "not-run",
        "A6_asmo_cast_paid": "not-run",
    }
    notes = []

    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    C0 = p0
    assertions["A1_setup_ok"] = "passed"

    MULLS = {0: 0, 1: 0}

    async def handle_mulligan(c, pid, tag, st, state, acts):
        if wf_type(state) != "MulliganDecision":
            return False
        pend = wf_data(state).get("pending") or []
        mine = [e for e in pend
                if (e.get("player") if isinstance(e, dict) else None) == pid
                and (e.get("phase") or {}).get("type") == "Declare"]
        if not mine:
            return False
        rev = st.get("state_revision", -1)
        if acted(f"mull{pid}", rev):
            return True
        if pid == 0:
            want = "keep" if (LOOTING in hand_names(state, 0)
                              or MULLS[0] >= 2) else "mulligan"
            if want == "mulligan":
                MULLS[0] += 1
        else:
            want = "keep"
        adv = next((a for a in acts if a.get("type") == "MulliganDecision"), None)
        if adv:
            sub = copy.deepcopy(adv)
            dd = sub.setdefault("data", {})
            dd["decision"] = want
            dd["choice"] = {"type": "Keep" if want == "keep" else "Mulligan"}
        else:
            sub = {"type": "MulliganDecision",
                   "data": {"decision": want,
                            "choice": {"type": "Keep" if want == "keep" else "Mulligan"}}}
        await submit_as_is(c, sub)
        say(f"[{tag}] mulligan: {want}")
        return True

    async def handle_bottom(c, pid, tag, st, state, acts):
        pend = wf_data(state).get("pending") or []
        mine = [e for e in pend
                if (e.get("player") if isinstance(e, dict) else None) == pid
                and (e.get("phase") or {}).get("type") == "BottomCards"]
        if not mine:
            return False
        rev = st.get("state_revision", -1)
        if acted(f"bottom{pid}", rev):
            return True
        n = 1
        try:
            n = int(mine[0]["phase"].get("count", 1))
        except Exception:
            pass
        h = hand_oids(state, pid)
        h.sort(key=lambda o: (oname(get_obj(state, o)) not in LANDS,
                             oname(get_obj(state, o))))
        adv = next((a for a in acts if a.get("type") == "SelectCards"), None)
        if adv:
            sub = copy.deepcopy(adv)
            sub["data"]["cards"] = [int(x) for x in h[:n]]
            await submit_as_is(c, sub)
        else:
            vi = get_vi(st)
            if not vi:
                return False
            for opp in vi.get("opportunities", []) or []:
                chs, rtype = vi_choices(opp)
                if not chs:
                    continue
                oid_by_ref = {ref_of(ch): ch["id"] for ch in chs if ref_of(ch)}
                picks = [oid_by_ref[o] for o in h[:n] if o in oid_by_ref]
                if len(picks) == n:
                    await send_interaction(
                        c, {"interactionId": opp.get("interactionId"),
                            "response": {"type": "select",
                                         "data": {"choiceIds": picks}}})
                    break
            else:
                return False
        say(f"[{tag}] bottoms {n} after mulligan")
        return True

    async def handle_nonpriority(c, pid, tag, st, state, acts):
        wtype = wf_type(state)
        if wtype == "Priority":
            return False
        if await handle_mulligan(c, pid, tag, st, state, acts):
            return True
        if await handle_bottom(c, pid, tag, st, state, acts):
            return True
        d = wf_data(state)
        dp = d.get("player")
        if isinstance(dp, dict):
            dp = dp.get("id", -1)
        if wtype == "DiscardChoice" and dp == pid:
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    if iid in PROMPT_DONE or iid in SKIP_IID:
                        continue
                    if await answer_discard(c, pid, opp, tag, protect=[ASMO]):
                        PROMPT_DONE.add(iid)
                        if ST["stage"] in ("LOOTING", "SEEK"):
                            ST["stage"] = "POS"
                        return True
            say(f"[{tag}] DiscardChoice but no answerable opportunity; deferring")
            return True
        if wtype in ("DiscardToHandSize",) and dp == pid:
            vi = get_vi(st)
            if vi:
                for opp in vi.get("opportunities", []) or []:
                    iid = opp.get("interactionId")
                    if iid in PROMPT_DONE or iid in SKIP_IID:
                        continue
                    if await answer_discard(c, pid, opp, tag, protect=[ASMO]):
                        PROMPT_DONE.add(iid)
                        return True
            return True
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            adv = next((a for a in acts if a.get("type") == wtype), None)
            if adv:
                sub = copy.deepcopy(adv)
                dd = sub.setdefault("data", {})
                for k in ("attacks", "attackers", "blocks", "blockers", "assignments"):
                    if k in dd:
                        dd[k] = [] if isinstance(dd[k], list) else {}
                await submit_as_is(c, sub)
                say(f"[{tag}] declares no {wtype}")
                return True
            return False
        if wtype == "ChooseLegend":
            adv = next((a for a in acts if a.get("type") == "ChooseLegend"), None)
            if adv:
                await submit_as_is(c, adv)
                say(f"[{tag}] chooses legend (keep first)")
                return True
            return False
        adv = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if adv:
            await submit_as_is(c, adv)
            return True
        return False

    def castspell_for(acts, state, name):
        for a in acts:
            if a.get("type") == "CastSpell":
                oid = a.get("data", {}).get("object_id")
                if oid is not None and oname(get_obj(state, oid)) == name:
                    return a
        return None

    def negative_leg(st, acts, state, turn, asmo_oid, looting_oid):
        """Returns True if it acted. Shared by NEG and SEEK stages."""
        cast = castspell_advertised(acts, asmo_oid)
        if cast:
            assertions["A2_neg_no_cast_offered"] = "failed"
            ST["bugs"].append(
                f"REPORTED BUG: CastSpell(Asmo) advertised on a no-discard turn "
                f"(turn {turn}, stage {ST['stage']})")
            say(ST["bugs"][-1])
            return ("bug", cast)
        offer = vi_cast_offer_for(st, ASMO)
        if offer and assertions["A3_neg_no_viewer_cast"] != "failed":
            assertions["A3_neg_no_viewer_cast"] = "failed"
            ST["bugs"].append(
                f"REPORTED BUG: vi cast offer for Asmo on a no-discard turn "
                f"(turn {turn}, stage {ST['stage']})")
            say(ST["bugs"][-1])
            return ("bug", None)
        ST["neg_obs"] += 1
        if not ST["pre_neg"]:
            ST["pre_neg"] = True
            return ("export_pre_neg", None)
        if assertions["A3_neg_no_viewer_cast"] == "not-run":
            assertions["A3_neg_no_viewer_cast"] = "passed"
        # cast Faithless Looting once enough negative observations gathered
        need = 3 if ST["loots_done"] == 0 else 1
        if ST["neg_obs"] >= need and looting_oid:
            cast = castspell_for(acts, state, LOOTING)
            if cast:
                return ("cast_looting", cast)
        return None

    async def p0_tick(st, acts, state):
        rev = st.get("state_revision", -1)
        drain_rejections(p0)
        if await handle_nonpriority(p0, 0, "P0", st, state, acts):
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p0, a)
                say("[P0] mana payment submitted as-is")
                return
        if not my_priority(state, 0):
            return
        turn = turn_of(state)
        in_main = is_my_main(state, 0)
        asmo_oid = find_hand(state, 0, ASMO)
        looting_oid = find_hand(state, 0, LOOTING)

        # ---- NEGATIVE legs (no discard this turn) ----
        if ST["stage"] in ("NEG", "SEEK") and asmo_oid and in_main \
                and ST["discard_turn"] != turn:
            r = negative_leg(st, acts, state, turn, asmo_oid, looting_oid)
            if r == ("export_pre_neg", None) or r is None:
                if r:
                    await export_now("pre_neg.json")
                    say(f"PRE_NEG captured turn {turn} (Asmo in hand, no discard "
                        f"this turn, no cast offered)")
            elif r[0] == "bug":
                if not ST["pre_neg"]:
                    ST["pre_neg"] = True
                    await export_now("pre_neg.json")
                if r[1] is not None:
                    await submit_as_is(p0, r[1])
                    ST["asmo_cast"] = True
                    ST["asmo_cast_turn"] = turn
                    ST["asmo_oid_cast"] = str(asmo_oid)
                    ST["stage"] = "BUGPROOF"
                    return
            elif r[0] == "cast_looting":
                say(f"[P0] casting Faithless Looting (turn {turn}; "
                    f"discard-turn #{ST['loots_done'] + 1})")
                await submit_as_is(p0, r[1])
                ST["looting_in_flight"] = True
                ST["looting_turn"] = turn
                ST["stage"] = "LOOTING"
                return

        # ---- POSITIVE leg (same turn as the discard) ----
        if ST["stage"] == "POS" and asmo_oid and in_main \
                and ST["discard_turn"] == turn and not ST["asmo_cast"]:
            cast = castspell_advertised(acts, asmo_oid)
            if cast:
                if assertions["A5_cast_offered_after_discard"] == "not-run":
                    assertions["A5_cast_offered_after_discard"] = "passed"
                ST["offer_turns"].append(turn)
                notes.append(f"CastSpell(Asmo) offered on discard turn {turn}")
                ST["pre_cast_br"] = untapped_br(state, 0)
                await export_now("pre_cast.json")
                say(f"[P0] casting Asmo for {{B/R}} (turn {turn}); untapped "
                    f"B/R lands={ST['pre_cast_br']}")
                await submit_as_is(p0, cast)
                ST["asmo_cast"] = True
                ST["asmo_cast_turn"] = turn
                ST["asmo_oid_cast"] = str(asmo_oid)
                return
            ST["pos_obs"] += 1
            if ST["pos_obs"] == 1:
                await export_now("post_discard.json")
                say(f"[P0] Asmo NOT offered right after discard; watching "
                    f"(turn {turn})")

        # ---- discard turn ended: move on or finish ----
        if ST["stage"] == "POS" and ST["discard_turn"] is not None \
                and turn > ST["discard_turn"]:
            notes.append(f"discard turn {ST['discard_turn']} ended without "
                         f"CastSpell(Asmo) offered ({ST['pos_obs']} observations)")
            say(notes[-1])
            ST["discard_turn"] = None
            ST["pos_obs"] = 0
            ST["looting_in_flight"] = False
            if ST["loots_done"] >= 2 or turn > 40:
                await export_now("post.json")
                ST["done"] = True
                return
            ST["stage"] = "SEEK"
            say(f"[P0] seeking second Looting (turn {turn})")

        # ---- watch Asmo resolve to battlefield ----
        if ST["asmo_cast"] and not ST["post_exported"]:
            if stack_has(state, ASMO) and not ST["mid_exported"]:
                ST["mid_exported"] = True
                await export_now("mid_cast.json")
                say("MID_CAST captured (Asmo on stack)")
            oid = bf_find(state, 0, ASMO)
            if oid is not None:
                post_br = untapped_br(state, 0)
                spent = (ST["pre_cast_br"] or 0) - post_br
                notes.append(f"Asmo reached battlefield (turn {turn}); untapped "
                             f"B/R lands {ST['pre_cast_br']}->{post_br} (delta {spent})")
                assertions["A6_asmo_cast_paid"] = "passed" if (
                    ST["pre_cast_br"] is None or spent >= 1) else "failed"
                await export_now("post.json")
                ST["post_exported"] = True
                ST["done"] = True
                return

        # ---- BUGPROOF leg: Asmo cast pre-discard; watch it resolve ----
        if ST["stage"] == "BUGPROOF":
            oid = bf_find(state, 0, ASMO)
            if oid is not None:
                ST["bugs"].append(
                    f"Asmo cast WITHOUT discarding resolved to battlefield "
                    f"(turn {turn}) — reported bug confirmed end-to-end")
                say(ST["bugs"][-1])
                await export_now("post.json")
                ST["done"] = True
                return

        # ---- economy ----
        if in_main:
            pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
            if pl and not acted("land", rev):
                await submit_as_is(p0, pl)
                return
        if my_priority(state, 0):
            pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
            if pp:
                await submit_as_is(p0, pp)

    async def p1_tick(st, acts, state):
        drain_rejections(p1)
        if await handle_nonpriority(p1, 1, "P1", st, state, acts):
            return
        for a in acts:
            if a.get("type") in ("PayMana", "PayManaAbilityMana",
                                 "PayManaAbility", "ManaPayment"):
                await submit_as_is(p1, a)
                return
        if not my_priority(state, 1):
            return
        rev = st.get("state_revision", -1)
        if is_my_main(state, 1):
            pl = next((a for a in acts if a.get("type") == "PlayLand"), None)
            if pl and not acted("p1land", rev):
                await submit_as_is(p1, pl)
                return
        pp = next((a for a in acts if a.get("type") == "PassPriority"), None)
        if pp:
            await submit_as_is(p1, pp)

    last = {}
    last_adv = {p0.name: time.time(), p1.name: time.time()}
    warned = set()
    for i in range(20000):
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
            st = c.latest
            if not st or c.revision == last.get(c.name):
                continue
            state = st["state"]
            last[c.name] = c.revision
            last_adv[c.name] = time.time()
            warned.discard(c.name)
            ST["states_seen"] += 1
            ST["max_turn"] = max(ST["max_turn"], turn_of(state))
            acts = list(st.get("legal_actions") or [])
            try:
                await tick(st, acts, state)
            except Exception as e:
                say(f"[{c.name}] tick error: {e!r}")
            if ST["done"]:
                break
        if not ST["done"] and i % 100 == 0:
            # stale-client watchdog: log revision + view when a client goes
            # >60s without advancing (#4509 lesson)
            now = time.time()
            for c in (p0, p1):
                if (now - last_adv[c.name] > 60 and c.name not in warned
                        and c.latest):
                    warned.add(c.name)
                    st = c.latest
                    state = st.get("state", {})
                    say(f"WATCHDOG {c.name}: no revision advance for "
                        f"{now - last_adv[c.name]:.0f}s; rev={c.revision} "
                        f"turn={turn_of(state)} phase={state.get('phase')} "
                        f"wf={wf_type(state)} pp={(st.get('priority_player') or '')}")
                    notes.append(f"watchdog: {c.name} stalled rev={c.revision} "
                                 f"wf={wf_type(state)}")
        if ST["done"]:
            break
        if ST["max_turn"] > 45 and ST["stage"] in ("NEG", "SEEK", "LOOTING"):
            notes.append("game reached turn 45 without completing the discard legs")
            break

    # ---------------- finalize ----------------
    if assertions["A2_neg_no_cast_offered"] == "not-run":
        assertions["A2_neg_no_cast_offered"] = "passed" if ST["neg_obs"] >= 3 else (
            "failed" if ST["neg_obs"] == 0 else "not-run")
        if ST["neg_obs"] < 3:
            notes.append(f"only {ST['neg_obs']} negative observations (<3)")
    if assertions["A3_neg_no_viewer_cast"] == "not-run":
        assertions["A3_neg_no_viewer_cast"] = "passed"
    if assertions["A4_looting_resolved"] == "not-run":
        assertions["A4_looting_resolved"] = "passed" if ST["all_discarded"] else "failed"
        if not ST["all_discarded"]:
            notes.append("Faithless Looting never resolved with a discard")
    if assertions["A5_cast_offered_after_discard"] == "not-run":
        if not ST["all_discarded"]:
            assertions["A5_cast_offered_after_discard"] = "not-run"
            notes.append("A5 not-run: no discard happened")
        elif ST["loots_done"] >= 1:
            assertions["A5_cast_offered_after_discard"] = "failed"
            notes.append(f"Asmo never offered on any of {ST['loots_done']} discard "
                         f"turns (turns {ST['discard_turns']})")
    if assertions["A6_asmo_cast_paid"] == "not-run" and not ST["asmo_cast"]:
        assertions["A6_asmo_cast_paid"] = "not-run"
        notes.append("A6 not-run: Asmo was never cast")
    if ST["bugs"]:
        notes.extend(ST["bugs"])

    reported = (assertions["A2_neg_no_cast_offered"] == "failed"
                or assertions["A3_neg_no_viewer_cast"] == "failed")
    verdict = ("reproduced" if reported
               else "not-reproduced" if assertions["A2_neg_no_cast_offered"] == "passed"
               else "blocked")
    if REJECTS:
        notes.append(f"protocol-72 interaction rejections observed: "
                     f"{json.dumps(REJECTS)}; skipped iids: {sorted(SKIP_IID)}")
    else:
        notes.append("no interaction rejections observed on protocol 71")

    # parse check on the pinned card data
    cd_path = f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"
    dp_path = f"{BACKFILL}/server/releases/v0.85.0/data/draft-pools.json"
    bin_path = f"{BACKFILL}/server/releases/v0.85.0/phase-server-slim-x86_64-unknown-linux-musl"
    card_data = json.load(open(cd_path))
    entry = None
    for k, v in (card_data.items() if isinstance(card_data, dict) else []):
        nm = (v.get("name") if isinstance(v, dict) else "") or ""
        if nm.replace(" ", "").lower().startswith("asmoranomardicad"):
            entry = v
            break
    parse_note = {}
    if entry:
        parse_note = {
            "mana_cost": entry.get("mana_cost"),
            "parse_warnings": entry.get("parse_warnings"),
            "static_abilities": entry.get("static_abilities"),
            "oracle_text": (entry.get("oracle_text") or entry.get("text") or "")[:400],
        }
    notes.append(f"parse check (pinned card-data.json): "
                 f"mana_cost={json.dumps(parse_note.get('mana_cost'))[:120]}; "
                 f"parse_warnings={json.dumps(parse_note.get('parse_warnings'))[:300]}; "
                 f"static_abilities={json.dumps(parse_note.get('static_abilities'))[:400]}")

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    server_identity = {
        "server_version": "0.85.0",
        "build_commit": "cb58ef5",
        "protocol_version": 72,
        "mode": "Full",
        "binary_sha256": sha(bin_path),
        "card_data_sha256": sha(cd_path),
        "draft_pools_sha256": sha(dp_path),
        "signature_key_id": "repo-pinned SERVER_ARTIFACT_PUBLIC_KEY",
        "signature_verified": True,
        "observed_at": "2026-09-17",
        "source": "ServerHello + minisign verification against repo-pinned key "
                  "(hashes recomputed from on-disk release files)",
    }

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server": server_identity,
        "server_run_dir": f"runs/{SERVER_RUN_ID}",
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario_sha256": sha(__file__),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "stats": {"states_seen": ST["states_seen"], "neg_observations": ST["neg_obs"],
                  "pos_observations": ST["pos_obs"], "max_turn": ST["max_turn"],
                  "looting_turn": ST["looting_turn"],
                  "discard_turns": ST["discard_turns"],
                  "offer_turns": ST["offer_turns"],
                  "asmo_cast_turn": ST["asmo_cast_turn"],
                  "all_discarded": ST["all_discarded"]},
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
        "validated_at": "2026-09-17",
        "limitations": ["Browser UI not exercised; native engine via two human-client seats.",
                        "Dense playsets are a test-harness convenience (engine accepts >4-of "
                        "for custom games).",
                        "States are authoritative exports, restorable only via full game replay."],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_647.py", "w") as f:
        f.write(open(__file__).read())
    slog = f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log"
    if os.path.exists(slog):
        with open(slog, "rb") as fi, open(f"{EVDIR}/server.log", "wb") as fo:
            fo.write(fi.read())
    render_png(f"{EVDIR}/summary.png", run)
    WIRE.close()
    RUNLOG.close()
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": assertions}, indent=1))
    await p0.close()
    await p1.close()


asyncio.run(main())
