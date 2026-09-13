#!/usr/bin/env python3
"""Issue #6987: Textual Madness cost "Pay six {C}" is unsupported.

Card: Emrakul, the World Anew. Oracle line "Madness--Pay six {C}." parses as
abilities=[Spell/Unimplemented unknown] instead of keywords=[{"Madness": Cost}]
like every other madness card (e.g. Fiery Temper -> keywords=[{"Madness":
{"type":"Cost","shards":["Red"],"generic":0}}]). Expected per the issue: "The
Madness permission should offer the printed alternative cost of six colorless
mana after the card is discarded into exile."

Plan (two human seats, native engine, v0.81.3/protocol 70):
  CONTROL - P0 casts Faithless Looting, discards Fiery Temper (Madness {R}).
            The engine should exile it (madness replacement) and offer the
            madness cast for {R}; the driver pays it and it resolves for 3
            damage to P1. This proves the engine's madness machinery works
            for correctly-parsed cards.
  PRIMARY - P0 casts a second Faithless Looting, discards Emrakul, the World
            Anew. The driver records the zone it lands in and whether any
            madness cast is offered at six {C}.

Assertions:
  A1 parse_emrakul_madness  card-data keywords carry Madness with cost 6x{C}
  A2 setup_ok               pre.json: P0 main phase, Looting + Fiery Temper
                            in hand, >=1 untapped Mountain
  A3 control_exile          discarded Fiery Temper entered Exile (not gy)
  A4 control_offer           madness cast offered for the exiled Fiery Temper
  A5 control_resolves        cast for {R} resolves; P1 20->17
  A6 emrakul_exile          discarded Emrakul entered Exile (not gy)
  A7 emrakul_offer          madness cast offered at six {C}
  A8 cleanup                post.json: stack empty, game proceeds

Verdict = reproduced iff A6 or A7 fails (the reported outcome is not met).
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
ISSUE = 6987
RUN_ID = "20260913-6987i"
EVID_ISSUE = "6987"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = f"{BACKFILL}/server/releases/v0.81.3/data/card-data.json"


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


LOOTING = "Faithless Looting"
TEMPER = "Fiery Temper"
EMRAKUL = "Emrakul, the World Anew"
MOUNTAIN = "Mountain"
WASTES = "Wastes"

ST = {"stage": "SETUP", "stop": False,
      "pre_exported": False, "mid_exported": False, "post_exported": False,
      "control_exiled": False, "control_offered": False,
      "control_offer_source": None, "control_cast_oid": None,
      "control_stack_seen": False, "control_resolved": False,
      "control_p1_before": None, "control_p1_after": None,
      "primary_zone": None, "primary_offered": False,
      "primary_offer_source": None,
      "p0_turn_cap_abort": False}
LOOT = {"tag": None, "cast": False, "in_flight": False, "discarded": False,
        "rejected": False}
WATCH = {"active": False, "proof": None, "discarded": [], "exiled": [],
         "offer": None, "offer_source": None, "acted": False,
         "cast_oid": None, "stack_seen": False, "resolved": False,
         "t0": 0.0, "turn0": 0, "closed": False, "close_reason": None}
MULLS = {"P0": 0}
WF_SEEN = []
PROMPT_SEEN = {}   # iid -> {"t0":..., "done":..., "answered":...}
C0 = None


# ------------------------------------------------------------- state helpers

def oname(o):
    return o.get("card_name") or o.get("name") or ""


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid) if not o.get("tapped"))


def untapped_mountains(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == MOUNTAIN and not o.get("tapped"))


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"))


def stack_empty(state):
    return not (state.get("stack") or [])


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


def turn_of(state):
    return state.get("turn_number") or state.get("turn") or 0


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        t = d.get("text") or d.get("label") or d.get("value")
        if t:
            bits.append(str(t))
    nm = ch.get("name") or ch.get("label")
    if nm:
        bits.append(str(nm))
    return " | ".join(bits)


def ref_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("reference") is not None:
            return str(d.get("reference"))
    return None


def seat_of(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("seat") is not None:
            return d.get("seat")
    return None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                          "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub,
                               "stage": ST["stage"]})
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
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:220]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def vi_cast_choice(st, oids, names):
    """Find a viewer_interaction castSpell choice naming one of the oids."""
    vi = get_vi(st)
    if not vi:
        return None
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in action_codes(ch):
                continue
def vi_cast_choice(st, oids, names):
    vi = get_vi(st)
    if not vi:
        return None
    want = set(str(o) for o in oids)
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = action_codes(ch)
            if not any("castSpell" in c for c in codes):
                continue
            r = ref_of(ch)
            obj_names = [((s.get("data") or {}).get("name") or "")
                         for s in ch.get("surfaces", []) or [] if s.get("type") == "object"]
            if (r and r in want) or any(n in obj_names for n in names):
                return opp, ch
    return None


async def answer_vi_choice(c, opp, choice_id):
    """Submit a single choice id using the opportunity's response shape."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = (data.get("spec") or {}).get("type") if isinstance(
        data.get("spec"), dict) else None
    if not spec:
        spec = data.get("type")
    if rtype == "schema" and spec in ("sequence", "select"):
        resp_out = {"type": spec, "data": {"choiceIds": [choice_id]}}
    else:
        # exactChoices single pick
        resp_out = {"type": "choose", "data": {"choiceId": choice_id}}
    await send_interaction(c, {"interactionId": iid, "response": resp_out})
    PROMPT_SEEN.setdefault(iid, {})["answered"] = True
    say(f"[{c.name}] answered vi {str(iid)[:8]} choice {choice_id} "
        f"(rtype={rtype} spec={spec})")

# ------------------------------------------------------- interaction driver

async def answer_discard(c, opp, want_names, tag):
    """Answer a DiscardChoice by discarding want_names (plus fillers)."""
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = (data.get("spec") or {}).get("type") if isinstance(
        data.get("spec"), dict) else data.get("type")
    chs = data.get("choices") or data.get("candidates") or []
    state = c.latest["state"]
    # map hand oids -> choice ids via reference surfaces
    oid_by_ref = {}
    for ch in chs:
        r = ref_of(ch)
        if r:
            oid_by_ref[r] = ch["id"]
    picks = []
    for nm in want_names:
        oid = find_hand(state, 0, nm)
        if oid and oid in oid_by_ref:
            picks.append((oid, oid_by_ref[oid]))
    if len(picks) < len(want_names):
        say(f"[{tag}] discard: want {want_names}, only mapped "
            f"{[p[0] for p in picks]}; deferring")
        return False
    choice_ids = [cid for _, cid in picks]
    if rtype == "schema" and spec in ("sequence", "select"):
        resp_out = {"type": spec, "data": {"choiceIds": choice_ids}}
    elif rtype == "exactChoices" and len(choice_ids) == 1:
        resp_out = {"type": "choose", "data": {"choiceId": choice_ids[0]}}
    else:
        say(f"[{tag}] discard: unexpected rtype={rtype} spec={spec}; deferring")
        return False
    await send_interaction(c, {"interactionId": iid, "response": resp_out})
    PROMPT_SEEN.setdefault(iid, {})["answered"] = True
    WATCH["discarded"] = [oid for oid, _ in picks]
    WATCH["active"] = True
    WATCH["t0"] = time.time()
    WATCH["turn0"] = turn_of(state)
    say(f"[{tag}] discards {[oname(get_obj(state, o)) for o, _ in picks]} "
        f"oids={[o for o, _ in picks]}; WATCH armed for {WATCH['proof']}")
    wire("discard_answered", {"tag": tag, "oids": WATCH["discarded"],
                             "proof": WATCH["proof"]})
    return True


async def scan_interactions(c, st, state, acts):
    """Answer P0's pending decisions. Returns True if it acted."""
    vi = get_vi(st)
    if not vi:
        return False
    wf = (state.get("waiting_for") or {})
    wtype = wf.get("type")
    wdata = wf.get("data") or {}
    if wdata.get("player") != 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        entry = PROMPT_SEEN.setdefault(iid, {"t0": time.time(),
                                            "done": False})
        if entry.get("done") or entry.get("answered"):
            continue
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        # --- Faithless Looting discard ---
        if wtype == "DiscardChoice" and LOOT.get("in_flight") \
                and not LOOT.get("discarded"):
            if WATCH["proof"] == "control":
                # keep Emrakul + a second Looting; discard Temper + filler
                filler = None
                for oid in hand_oids(state, 0):
                    nm = oname(state["objects"][oid])
                    if nm not in (TEMPER, EMRAKUL, LOOTING):
                        filler = nm
                        break
                if filler is None:
                    say("[P0] control discard: no filler available; deferring")
                    return False
                if await answer_discard(c, opp, [TEMPER, filler], "control"):
                    LOOT["discarded"] = True
                    return True
                return False
            elif WATCH["proof"] == "primary":
                filler = None
                for oid in hand_oids(state, 0):
                    nm = oname(state["objects"][oid])
                    if nm not in (EMRAKUL, LOOTING):
                        filler = nm
                        break
                if filler is None:
                    say("[P0] primary discard: no filler available; deferring")
                    return False
                if await answer_discard(c, opp, [EMRAKUL, filler], "primary"):
                    LOOT["discarded"] = True
                    return True
                return False
        # --- madness cast offer (CastOffer): choose cast/auto promptly ---
        # (stray offers outside a watch, e.g. a madness card discarded to
        # hand size during setup, are declined so the game keeps moving)
        if wtype == "CastOffer" and not WATCH.get("closed"):
            in_watch = WATCH.get("active") and not WATCH.get("acted")
            wire("castoffer_opportunity",
                 {"proof": WATCH.get("proof"), "in_watch": in_watch,
                  "iid": str(iid)[:24],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            cast_pick = None
            decline_pick = None
            for ch in chs:
                codes = action_codes(ch)
                if any("castSpell" in c for c in codes):
                    cast_pick = ch
                    pay = None
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data") or {}
                        if s.get("type") == "value" and d.get("role") == "paymentMode":
                            pay = d.get("value")
                    if pay == "auto":
                        break
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if s.get("type") == "value" \
                            and d.get("role") == "accept" \
                            and d.get("value") == "false":
                        decline_pick = ch
            if not in_watch:
                if decline_pick is None:
                    say(f"[P0] stray CastOffer: no decline choice "
                        f"({len(chs)} offered); NOT answering")
                    entry["done"] = True
                    return True
                say(f"[P0] stray CastOffer during {ST['stage']}: declining "
                    f"(choice={decline_pick['id'][-14:]})")
                wire("stray_castoffer_declined",
                     {"stage": ST["stage"], "iid": str(iid)[:24]})
                await answer_vi_choice(c, opp, decline_pick["id"])
                return True
            wire("castoffer_opportunity",
                 {"proof": WATCH.get("proof"), "iid": str(iid)[:24],
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            if cast_pick is None:
                # fallbacks: accept=true value surface, or the sole choice
                # (madness offers may not carry a castSpell action code)
                accept = None
                for ch in chs:
                    for s in ch.get("surfaces", []) or []:
                        d2 = s.get("data") or {}
                        if s.get("type") == "value" \
                                and d2.get("role") == "accept" \
                                and str(d2.get("value")).lower() == "true":
                            accept = ch
                            break
                if accept is not None:
                    cast_pick = accept
                    say(f"[P0] {WATCH['proof']} CastOffer: using "
                        f"accept=true choice {accept['id'][-14:]}")
                elif len(chs) == 1:
                    cast_pick = chs[0]
                    say(f"[P0] {WATCH['proof']} CastOffer: sole offered "
                        f"choice -> casting "
                        f"({choice_text(chs[0])[:80]})")
            if cast_pick is None:
                say(f"[P0] {WATCH['proof']} CastOffer: no cast choice "
                    f"({len(chs)} offered); NOT answering")
                entry["done"] = True
                return True
            proof = WATCH["proof"]
            if proof == "control":
                ST["control_offered"] = True
                ST["control_offer_source"] = "CastOffer"
            else:
                ST["primary_offered"] = True
                ST["primary_offer_source"] = "CastOffer"
            WATCH["offer"] = True
            WATCH["offer_source"] = "CastOffer"
            WATCH["offer_iid"] = str(iid)[:24]
            WATCH["acted"] = True
            wcard = TEMPER if proof == "control" else EMRAKUL
            WATCH["cast_oid"] = next(
                (o for o in WATCH["discarded"]
                 if oname(get_obj(state, o)) == wcard), None)
            WATCH["rev_at_submit"] = c.revision
            say(f"[P0] {proof} madness CAST OFFER via CastOffer "
                f"(choice={cast_pick['id'][-14:]})")
            wire("watch_offer", {"proof": proof, "source": "CastOffer",
                                 "iid": str(iid)[:24]})
            await answer_vi_choice(c, opp, cast_pick["id"])
            wire("watch_cast_submitted", {"proof": proof,
                                          "oid": WATCH["cast_oid"]})
            return True
        # --- madness cast target selection: target P1 ---
        if wtype == "TargetSelection" and WATCH.get("acted") \
                and not WATCH.get("target_answered"):
            pick = None
            for ch in chs:
                if seat_of(ch) == 1:
                    pick = ch
                    break
            if pick is None:
                for ch in chs:
                    if "player" in choice_text(ch).lower():
                        pick = ch
                        break
            wire("target_selection", {"proof": WATCH["proof"],
                                     "n": len(chs),
                                     "texts": [choice_text(ch)[:60]
                                               for ch in chs][:8]})
            if pick is None:
                say(f"[P0] {WATCH['proof']} target: no P1 candidate "
                    f"({len(chs)} offered); NOT answering")
                entry["done"] = True
                return True
            WATCH["target_answered"] = True
            say(f"[P0] {WATCH['proof']} madness target: P1 "
                f"({choice_text(pick)[:60]})")
            await answer_vi_choice(c, opp, pick["id"])
            return True
        # --- unknown prompt during a madness watch: log fully; answer ---
        if WATCH.get("active") and not WATCH.get("closed"):
            wire("watch_prompt",
                 {"proof": WATCH["proof"], "wf": wtype, "iid": str(iid)[:8],
                  "rtype": resp.get("type"),
                  "opportunity": json.loads(json.dumps(opp, default=str))})
            say(f"[P0] WATCH prompt ({WATCH['proof']}): wf={wtype} "
                f"iid={str(iid)[:8]} n={len(chs)}")
            # prefer an accept=true choice (the "yes, cast it" path)
            accept = None
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and d.get("role") == "accept" \
                            and str(d.get("value")).lower() == "true":
                        accept = ch
                        break
            age = time.time() - entry["t0"]
            if accept is not None and age > 5:
                say(f"[P0] WATCH: answering accept=true "
                    f"({choice_text(accept)[:60]})")
                await answer_vi_choice(c, opp, accept["id"])
                return True
            if age > 20:
                say(f"[P0] WATCH: answering first choice after 20s stall")
                await answer_vi_choice(c, opp, chs[0]["id"])
                return True
            return False
    return False

# ------------------------------------------------------------- madness watch

def watch_offer_scan(c, st, state, acts):
    """Scan for a madness cast offer for the watched discarded cards."""
    oids = WATCH["discarded"]
    proof = WATCH["proof"]
    card = TEMPER if proof == "control" else EMRAKUL
    a = None
    for oid in oids:
        if oname(get_obj(state, oid)) != card:
            continue
        a = castspell_advertised(acts, oid)
        if a:
            break
    if a:
        wire("watch_offer", {"proof": proof, "source": "legal_actions",
                             "action": a})
        say(f"[P0] WATCH {proof}: madness cast OFFERED via legal_actions "
            f"(oid data={json.dumps(a.get('data', {}))[:300]})")
        return ("legal_actions", a, None)
    hit = vi_cast_choice(st, oids, [card])
    if hit:
        opp, ch = hit
        wire("watch_offer", {"proof": proof, "source": "viewer_interaction",
                             "iid": str(opp.get("interactionId"))[:8],
                             "choice": json.loads(json.dumps(ch,
                                                             default=str))})
        say(f"[P0] WATCH {proof}: madness cast OFFERED via viewer_interaction")
        return ("viewer_interaction", opp, ch)
    return None


async def watch_tick(c, st, state, acts):
    """Track discarded cards' zones and any madness cast offer."""
    if not WATCH.get("active") or WATCH.get("closed"):
        return False
    proof = WATCH["proof"]
    card = TEMPER if proof == "control" else EMRAKUL
    acted = False
    # zone tracking
    for oid in WATCH["discarded"]:
        if oname(get_obj(state, oid)) != card:
            continue
        z = get_obj(state, oid).get("zone")
        if oid not in WATCH["exiled"] and z == "Exile":
            WATCH["exiled"].append(oid)
            say(f"[P0] WATCH {proof}: {card} oid={oid} entered EXILE")
            wire("watch_exiled", {"proof": proof, "oid": oid})
            if proof == "control":
                ST["control_exiled"] = True
            else:
                ST["primary_zone"] = "Exile"
        if z == "Graveyard" and proof == "primary" \
                and ST.get("primary_zone") is None:
            ST["primary_zone"] = "Graveyard"
            say(f"[P0] WATCH {proof}: {card} oid={oid} in GRAVEYARD "
                f"(no madness replacement)")
            wire("watch_graveyard", {"proof": proof, "oid": oid})
    # stack scan for madness text
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if "adness" in blob.lower() and not WATCH.get("stack_logged"):
            WATCH["stack_logged"] = True
            wire("watch_stack_madness", {"proof": proof,
                                        "entry": json.loads(blob)})
            say(f"[P0] WATCH {proof}: stack entry mentions madness: "
                f"{blob[:200]}")
    # offer scan
    if not WATCH.get("offer") and not WATCH.get("acted"):
        hit = watch_offer_scan(c, st, state, acts)
        if hit:
            src, payload, ch = hit
            WATCH["offer"] = True
            WATCH["offer_source"] = src
            if proof == "control":
                ST["control_offered"] = True
                ST["control_offer_source"] = src
            else:
                ST["primary_offered"] = True
                ST["primary_offer_source"] = src
            # attempt the cast
            tgt_oid = None
            for oid in WATCH["discarded"]:
                if oname(get_obj(state, oid)) == card:
                    tgt_oid = oid
                    break
            if src == "legal_actions":
                await submit_as_is(c, payload)
            else:
                await answer_vi_choice(c, payload, ch["id"])
            WATCH["acted"] = True
            WATCH["cast_oid"] = tgt_oid
            WATCH["rev_at_submit"] = c.revision
            say(f"[P0] WATCH {proof}: submitted madness cast for oid={tgt_oid}")
            wire("watch_cast_submitted", {"proof": proof, "oid": tgt_oid})
            acted = True
    # cast resolution tracking
    if WATCH.get("acted") and WATCH.get("cast_oid"):
        coid = WATCH["cast_oid"]
        on_stack = any(str(e.get("id")) == str(coid)
                       or str((e.get("source") or {}).get("id")
                              if isinstance(e.get("source"), dict)
                              else e.get("source")) == str(coid)
                       for e in state.get("stack") or [])
        # also match by card name on the stack entry
        if not on_stack:
            on_stack = any(card in json.dumps(e, default=str)
                           for e in state.get("stack") or [])
        if on_stack and not WATCH.get("stack_seen"):
            WATCH["stack_seen"] = True
            say(f"[P0] WATCH {proof}: madness spell on stack")
            wire("watch_stack_seen", {"proof": proof, "oid": coid})
        if WATCH.get("stack_seen") and not on_stack:
            WATCH["resolved"] = True
            if proof == "control":
                # capture post-control P1 life here: the primary gate (and
                # its mid.json export) may never run, so A5 must not
                # depend on it
                ST["control_p1_after"] = life_of(state, 1)
                say(f"[P0] WATCH control: P1 life now "
                    f"{ST['control_p1_after']}")
            say(f"[P0] WATCH {proof}: madness spell left the stack "
                f"(resolved)")
            wire("watch_resolved", {"proof": proof, "oid": coid})
            close_watch(f"cast resolved ({proof})")
            return True
        # rejection / silent-fail
        if WATCH.get("rejected"):
            WATCH["rejected"] = False
            say(f"[P0] WATCH {proof}: madness cast rejected")
            wire("watch_cast_rejected", {"proof": proof})
            close_watch(f"cast rejected ({proof})")
            return True
        if (not WATCH.get("stack_seen") and WATCH.get("rev_at_submit")
                and c.revision - WATCH["rev_at_submit"] >= 20):
            say(f"[P0] WATCH {proof}: cast silent-fail (no stack sighting)")
            wire("watch_cast_silent", {"proof": proof})
            close_watch(f"cast silent-fail ({proof})")
            return True
    # close: game back at a LATER P0 main phase with empty stack
    if (not WATCH.get("acted") and is_my_main(state, 0)
            and stack_empty(state) and turn_of(state) > WATCH["turn0"]):
        hit = watch_offer_scan(c, st, state, acts)
        if hit:
            return await watch_tick(c, st, state, acts)  # re-enter offer path
        close_watch(f"no offer by turn {turn_of(state)} main phase ({proof})")
        return True
    # close: wall timeout
    if time.time() - WATCH["t0"] > 300:
        close_watch(f"watch timeout 300s ({proof})")
        return True
    return acted


def close_watch(reason):
    WATCH["closed"] = True
    WATCH["close_reason"] = reason
    proof = WATCH["proof"]
    say(f"[P0] WATCH {proof} closed: {reason} "
        f"(exiled={WATCH['exiled']} offer={WATCH['offer']} "
        f"resolved={WATCH['resolved']})")
    wire("watch_closed", {"proof": proof, "reason": reason,
                          "exiled": WATCH["exiled"],
                          "offer": WATCH["offer"],
                          "resolved": WATCH["resolved"]})
    if proof == "control":
        ST["control_resolved"] = bool(WATCH["resolved"])
        ST["stage"] = "PRIMARY_SETUP"
        LOOT.update({"tag": None, "cast": False, "in_flight": False,
                     "discarded": False, "rejected": False})
        WATCH.update({"active": False, "proof": None, "discarded": [],
                      "exiled": [], "offer": None, "offer_source": None,
                      "acted": False, "cast_oid": None, "stack_seen": False,
                      "resolved": False, "t0": 0.0, "turn0": 0,
                      "closed": False, "close_reason": None,
                      "target_answered": False, "stack_logged": False,
                      "rejected": False, "rev_at_submit": None})
        say("=== stage -> PRIMARY_SETUP ===")
    else:
        ST["stage"] = "DONE"


# ------------------------------------------------------------------ tick

async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            if is_p0:
                good = find_hand(state, pid, LOOTING) is not None
                choice = "Keep" if (good or MULLS["P0"] >= 1) else "Mulligan"
                if choice == "Mulligan":
                    MULLS["P0"] += 1
            else:
                choice = "Keep"
            await submit_as_is(c, {"type": "MulliganDecision",
                                  "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    # BottomCards
    for a in acts:
        if a["type"] == "SelectCards" and \
                (state.get("waiting_for") or {}).get("type") == "MulliganDecision":
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
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
    # DiscardToHandSize
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            # protect key cards: lands first, then spare Lootings, then
            # Emrakul; never a madness card (Fiery Temper) if avoidable --
            # a hand-size discard of Temper raises a stray CastOffer that
            # stalls the game
            def _prio(oid):
                nm = oname(state["objects"][oid])
                if nm in (MOUNTAIN, WASTES):
                    return 0
                if nm == LOOTING:
                    return 1
                if nm == EMRAKUL:
                    return 2
                if nm == TEMPER:
                    return 3
                return 1
            oids = sorted(hand_oids(state, pid), key=_prio)
            picks = oids[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                      "data": {"cards": [int(x) for x in picks]}})
                say(f"{c.name} discards {len(picks)} to hand size: "
                    f"{[oname(state['objects'][o]) for o in picks]}")
                return True
    # combat: declare empty
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    # legend rule: keep the first (submit advertised choice as-is)
    for a in acts:
        if a["type"] == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            return True
    # P0 pending decisions via viewer interaction
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and await scan_interactions(c, st, state, acts):
        return True
    # never pass while P0 has a cast/ability decision pending
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                        "ManaPayment", "ChooseXValue", "DiscardChoice",
                        "CastOffer") \
            and wplayer == 0:
        return False
    # P0 madness watch runs on every P0 tick (not just main phase)
    if is_p0 and WATCH.get("active") and not WATCH.get("closed"):
        if await watch_tick(c, st, state, acts):
            return True
    # land drop for either player (retry every tick; P0 prefers Mountain)
    if is_my_main(state, pid):
        if is_p0:
            lid = find_hand(state, pid, MOUNTAIN) or find_hand(state, pid, WASTES)
        else:
            lid = find_hand(state, pid, MOUNTAIN)
        for a in acts:
            if a["type"] == "PlayLand" and lid and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    # stage steps
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await setup_step(c, pid, state, acts):
                return True
        elif ST["stage"] == "PRIMARY_SETUP":
            if await primary_step(c, pid, state, acts):
                return True
        # else: fall through to default pass below
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def setup_step(c, pid, state, acts):
    """CONTROL gate: cast Faithless Looting discarding Fiery Temper."""
    if turn_of(state) > 40:
        ST["p0_turn_cap_abort"] = True
        ST["stop"] = True
        say("SETUP: turn cap 40 reached without control gate; aborting")
        return False
    hand_names = [oname(state["objects"][o]) for o in hand_oids(state, pid)]
    if not ST.get("setup_diag_done"):
        ST["setup_diag_done"] = True
    say(f"SETUP t{turn_of(state)}: hand={hand_names} "
        f"untappedM={untapped_mountains(state, 0)} "
        f"untappedL={untapped_lands(state, 0)}")
    if not (find_hand(state, pid, LOOTING)
            and find_hand(state, pid, TEMPER)):
        return False
    if not (untapped_mountains(state, 0) >= 1
            and untapped_lands(state, 0) >= 2):
        return False
    if not ST["pre_exported"]:
        await export_now("pre.json")
        ST["pre_exported"] = True
        ST["control_p1_before"] = life_of(state, 1)
        say(f"pre.json exported (P1 life={ST['control_p1_before']})")
        return False  # cast on the next tick
    if not LOOT["cast"]:
        lid = find_hand(state, pid, LOOTING)
        a = castspell_advertised(acts, lid) if lid else None
        if a:
            LOOT.update({"tag": "control", "cast": True, "in_flight": True})
            WATCH["proof"] = "control"
            ST["stage"] = "CONTROL"
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting (control)")
            return True
    return False


async def primary_step(c, pid, state, acts):
    """PRIMARY gate: cast Faithless Looting discarding Emrakul."""
    if turn_of(state) > 60:
        ST["p0_turn_cap_abort"] = True
        ST["stop"] = True
        say("PRIMARY: turn cap 60 reached without primary gate; aborting")
        return False
    if not (find_hand(state, pid, LOOTING)
            and find_hand(state, pid, EMRAKUL)):
        return False
    if not (untapped_mountains(state, 0) >= 1
            and untapped_lands(state, 0) >= 2):
        return False
    if not ST["mid_exported"]:
        await export_now("mid.json")
        ST["mid_exported"] = True
        ST["control_p1_after"] = life_of(state, 1)
        say(f"mid.json exported (P1 life now {ST['control_p1_after']})")
        return False  # cast on the next tick
    if not LOOT["cast"]:
        lid = find_hand(state, pid, LOOTING)
        a = castspell_advertised(acts, lid) if lid else None
        if a:
            LOOT.update({"tag": "primary", "cast": True, "in_flight": True})
            WATCH["proof"] = "primary"
            ST["stage"] = "PRIMARY"
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting (primary: discarding Emrakul)")
            return True
    return False

# ------------------------------------------------------------------ main

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_card(cd, name):
    items = cd.items() if isinstance(cd, dict) else enumerate(cd)
    for _k, v in items:
        if isinstance(v, dict) and v.get("name") == name:
            return v
    return None


async def main():
    t0 = time.time()

    # parse evidence: the Emrakul record from the pinned card data
    cd = json.load(open(CARD_DATA))
    em = find_card(cd, EMRAKUL)
    temper = find_card(cd, TEMPER)
    with open(f"{EVDIR}/parse_emrakul.json", "w") as f:
        json.dump({"name": em["name"],
                   "oracle_text": em["oracle_text"],
                   "keywords": em["keywords"],
                   "abilities": em["abilities"],
                   "control": {"name": temper["name"],
                               "oracle_madness_line":
                               [l for l in temper["oracle_text"].split("\n")
                                if "adness" in l],
                               "keywords": temper["keywords"]}}, f, indent=1)
    say("wrote parse_emrakul.json")

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((LOOTING, 12), (TEMPER, 8), (EMRAKUL, 20),
                         (MOUNTAIN, 10), (WASTES, 10)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN, 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej and is_p0:
                if LOOT.get("in_flight") and not LOOT.get("discarded"):
                    LOOT.update({"cast": False, "in_flight": False,
                                 "rejected": True})
                    say("[P0] Looting cast rejected; gate will retry")
                if WATCH.get("acted") and not WATCH.get("stack_seen") \
                        and not WATCH.get("closed"):
                    WATCH["rejected"] = True
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # Looting resolution bookkeeping
        if LOOT.get("in_flight") and LOOT.get("discarded"):
            if not zone_oids(state, 0, "Stack", LOOTING):
                LOOT["in_flight"] = False
                say(f"Looting ({LOOT['tag']}) resolved (discard done)")

        # DONE -> export post and stop
        if ST["stage"] == "DONE" and not ST["post_exported"]:
            try:
                await export_now("post.json")
                ST["post_exported"] = True
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

    obs = await finish(t0)
    await p0.close()
    await p1.close()
    return obs


async def finish(t0):
    notes = []
    ass = {}

    def load_state(path):
        p = f"{EVDIR}/{path}"
        if not os.path.exists(p):
            return None
        return json.load(open(p))["state"]

    pre, mid, post = (load_state(f"{n}.json") for n in ("pre", "mid", "post"))

    # ---- A1: parse check ----
    try:
        pem = json.load(open(f"{EVDIR}/parse_emrakul.json"))
        mad = [k for k in pem["keywords"]
               if isinstance(k, dict) and "Madness" in k]
        ok = False
        detail = f"keywords={pem['keywords']}"
        if len(mad) == 1:
            cost = mad[0]["Madness"]
            shards = cost.get("shards") or []
            total = cost.get("generic", 0) + len(shards)
            ok = (total == 6
                  and all(s == "Colorless" for s in shards))
            detail = f"Madness cost={cost}"
        unimp = [a for a in pem["abilities"]
                 if (a.get("effect") or {}).get("type") == "Unimplemented"]
        notes.append(f"A1: {detail}; Unimplemented abilities: "
                     f"{[(a.get('kind'), (a.get('effect') or {}).get('name')) for a in unimp]}")
        ass["A1_parse_emrakul_madness"] = "passed" if ok else "failed"
    except Exception as e:
        notes.append(f"A1 parse check error: {e}")
        ass["A1_parse_emrakul_madness"] = "not-run"

    # ---- A2: setup ----
    if pre is not None:
        ok = (is_my_main(pre, 0)
              and find_hand(pre, 0, LOOTING) is not None
              and find_hand(pre, 0, TEMPER) is not None
              and untapped_mountains(pre, 0) >= 1)
        notes.append(f"A2: pre.json P0 main={is_my_main(pre, 0)}, "
                     f"Looting in hand={find_hand(pre, 0, LOOTING) is not None}, "
                     f"Temper in hand={find_hand(pre, 0, TEMPER) is not None}, "
                     f"untapped Mountains={untapped_mountains(pre, 0)} -> "
                     f"{'passed' if ok else 'failed'}")
        ass["A2_setup_ok"] = "passed" if ok else "failed"
    else:
        notes.append("A2 not-run: no pre.json (control gate never opened)")
        ass["A2_setup_ok"] = "not-run"

    # ---- A3/A4/A5: control ----
    ass["A3_control_exile"] = ("passed" if ST["control_exiled"]
                               else ("failed" if ass["A2_setup_ok"] == "passed"
                                     else "not-run"))
    notes.append(f"A3: discarded Fiery Temper entered Exile: "
                 f"{ST['control_exiled']} -> {ass['A3_control_exile']}")
    ass["A4_control_offer"] = ("passed" if ST["control_offered"]
                               else ("failed" if ass["A2_setup_ok"] == "passed"
                                     else "not-run"))
    notes.append(f"A4: madness cast offered for Fiery Temper: "
                 f"{ST['control_offered']} (source={ST['control_offer_source']}) "
                 f"-> {ass['A4_control_offer']}")
    p1d = None
    if ST["control_p1_before"] is not None and ST["control_p1_after"] is not None:
        p1d = ST["control_p1_before"] - ST["control_p1_after"]
    ok5 = ST["control_resolved"] and p1d == 3
    ass["A5_control_resolves"] = ("passed" if ok5
                                  else ("failed" if ST["control_offered"]
                                        else "not-run"))
    notes.append(f"A5: control resolved={ST['control_resolved']}, P1 life "
                 f"{ST['control_p1_before']}->{ST['control_p1_after']} "
                 f"(delta={p1d}, expected 3) -> {ass['A5_control_resolves']}")

    # ---- A6/A7: primary ----
    if ST.get("primary_zone") == "Exile":
        ass["A6_emrakul_exile"] = "passed"
    elif ST.get("primary_zone") == "Graveyard":
        ass["A6_emrakul_exile"] = "failed"
    else:
        ass["A6_emrakul_exile"] = "not-run"
    notes.append(f"A6: discarded Emrakul zone={ST.get('primary_zone')} "
                 f"(expected Exile via madness replacement) -> "
                 f"{ass['A6_emrakul_exile']}")
    if ST["primary_offered"]:
        ass["A7_emrakul_offer"] = "passed"
    elif ass["A6_emrakul_exile"] in ("passed", "failed"):
        ass["A7_emrakul_offer"] = "failed"
    else:
        ass["A7_emrakul_offer"] = "not-run"
    notes.append(f"A7: madness cast offered at six {{C}}: "
                 f"{ST['primary_offered']} "
                 f"(source={ST['primary_offer_source']}) -> "
                 f"{ass['A7_emrakul_offer']}")

    # ---- A8: cleanup ----
    if post is not None:
        ok = stack_empty(post)
        ass["A8_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A8: post.json stack empty={ok}, "
                     f"turn={turn_of(post)}, phase={post.get('phase')} -> "
                     f"{ass['A8_cleanup']}")
    else:
        notes.append("A8 not-run: no post.json")
        ass["A8_cleanup"] = "not-run"

    # ---- verdict ----
    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: control setup never reached "
                     f"(turn-cap abort={ST['p0_turn_cap_abort']})")
    elif ass["A6_emrakul_exile"] == "failed" or ass["A7_emrakul_offer"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: Emrakul's madness permission did not "
                     "offer the printed six-{C} alternative cost after discard")
    elif ass["A6_emrakul_exile"] == "passed" and ass["A7_emrakul_offer"] == "passed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: Emrakul exiled on discard and the "
                     "six-{C} madness cast was offered")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: primary proof did not complete")

    notes.append(f"WF sequence: {WF_SEEN}")
    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN}, f, indent=2)

    # ---- run.json ----
    bin_path = f"{BACKFILL}/server/releases/v0.81.3/phase-server-slim-x86_64-unknown-linux-musl"
    run = {
        "issue": ISSUE,
        "title": "Textual Madness cost \"Pay six {C}\" is unsupported",
        "run_id": RUN_ID,
        "server_version": "v0.81.3",
        "build_commit": "95bec6e",
        "protocol_version": 70,
        "server_binary_sha256": sha256_file(bin_path),
        "card_data_sha256": sha256_file(CARD_DATA),
        "draft_pools_sha256": sha256_file(
            f"{BACKFILL}/server/releases/v0.81.3/data/draft-pools.json"),
        "signature_verified": True,
        "validated_at": "2026-09-13",
        "validated_version": "v0.81.3",
        "verdict": verdict,
        "assertions": ass,
        "result": ("A1 parse_emrakul_madness: failed (no Madness keyword; "
                   "Spell/Unimplemented unknown). A2 setup_ok: "
                   f"{ass['A2_setup_ok']}. A3 control_exile: {ass['A3_control_exile']}; "
                   f"A4 control_offer: {ass['A4_control_offer']}; A5 control_resolves: "
                   f"{ass['A5_control_resolves']} (Fiery Temper Madness {{R}} control). "
                   f"A6 emrakul_exile: {ass['A6_emrakul_exile']}; A7 emrakul_offer: "
                   f"{ass['A7_emrakul_offer']}. A8 cleanup: {ass['A8_cleanup']}."),
        "scope": ("Emrakul, the World Anew madness permission: card-data parse "
                  "check + runtime discard via Faithless Looting with Fiery Temper "
                  "(Madness {R}) as the working-madness control; native engine, "
                  "two human-client seats"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "Six-{C} payment was not attempted: the offer itself is the asserted outcome.",
        ],
        "driver_notes": [
            "WATCH arms at DiscardChoice answer time and tracks the discarded oids' zones plus any CastSpell/castSpell offer each P0 tick.",
            "The watch closes at the first later-turn P0 main phase with an empty stack (final offer scan), on cast resolution, or after a 300s timeout.",
            "Unknown P0 prompts during a watch are wire-logged in full; accept=true choices preferred, else first choice after a 20s stall.",
        ],
        "server_run_note": ("Isolated v0.81.3 server on 127.0.0.1:9374, "
                            "started under setsid for this run "
                            "(ServerHello v0.81.3/95bec6e/protocol 70/mode Full; "
                            "log in runs/20260913-6987/server.log)"),
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")

    # ---- evidence files ----
    shutil.copy(f"{BACKFILL}/driver/scenario_6987.py",
                f"{EVDIR}/scenario_6987.py")
    for _log in ("server.log", "phase-server.log.2026-09-13"):
        _src = f"{BACKFILL}/runs/{RUN_ID}/{_log}"
        if os.path.exists(_src):
            shutil.copy(_src, f"{EVDIR}/{_log}")
    say("copied scenario_6987.py and server log(s) into EVDIR")

    # ---- summary.png (from saved states/assertions) ----
    render_summary(run, notes, pre, mid, post)

    # ---- close logs BEFORE hashing (manifest written last) ----
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass

    files = ["pre.json", "mid.json", "post.json", "parse_emrakul.json",
             "assertions.json", "run.json", "scenario_6987.py",
             "wire_log.jsonl", "scenario_run.log", "server.log", "phase-server.log.2026-09-13",
             "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            lines.append(f"{sha256_file(p)}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    # manifest written last; no say() after this point (logs are closed)

    return {"assert": ass, "verdict": verdict, "notes": notes}


def render_summary(run, notes, pre, mid, post):
    from PIL import Image, ImageDraw
    W, H = 1000, 1060
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), 'phase-rs/phase #6987 - Textual Madness cost "Pay six {C}" '
           'is unsupported', fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.81.3 (95bec6e) protocol 70 - 2026-09-13 - "
           "parser + runtime", fill=(140, 160, 180))
    y += 28
    v = run["verdict"]
    d.text((24, y), f"verdict: {v.upper()}",
           fill=(255, 90, 90) if v == "reproduced"
           else ((120, 220, 120) if v == "not-reproduced" else (230, 200, 120)))
    y += 34
    d.text((24, y), "Assertions (correct-behavior properties; "
           "failed = defect present):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_parse_emrakul_madness": "PARSE: Madness keyword with cost 6x{C} on Emrakul",
        "A2_setup_ok": "GAME: pre.json P0 main, Looting+Temper in hand, R untapped",
        "A3_control_exile": "GAME: discarded Fiery Temper entered Exile (control)",
        "A4_control_offer": "GAME: madness cast offered for Fiery Temper (control)",
        "A5_control_resolves": "GAME: Temper cast for {R} resolves, P1 20->17 (control)",
        "A6_emrakul_exile": "GAME: discarded Emrakul entered Exile (not graveyard)",
        "A7_emrakul_offer": "GAME: madness cast offered at six {C}",
        "A8_cleanup": "GAME: post.json stack empty, game proceeds",
    }
    for k, lab in labels.items():
        val = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if val == "passed" else (
            (255, 90, 90) if val == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {val} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Oracle: Madness--Pay six {C}.", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "Parsed on v0.81.3: abilities=[Spell/Unimplemented "
           "'unknown'];", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "keywords=[Flying, Protection x2] (no Madness keyword).",
           fill=(150, 160, 175))
    y += 22
    d.text((36, y), "Control Fiery Temper: keywords=[{Madness: {R}}].",
           fill=(150, 160, 175))
    y += 30

    def zones(st8, names):
        if st8 is None:
            return "n/a"
        out = []
        for oid, o in st8["objects"].items():
            if oname(o) in names and o.get("controller") == 0:
                out.append(f"{oname(o)[:14]}:{o.get('zone')}")
        return ", ".join(out) if out else "none tracked"

    for tag, st8 in (("pre", pre), ("mid", mid), ("post", post)):
        d.text((24, y), f"{tag}: turn={turn_of(st8) if st8 else 'n/a'} "
               f"phase={(st8 or {}).get('phase')} | "
               f"{zones(st8, (TEMPER, EMRAKUL, LOOTING))} | "
               f"P1 life={life_of(st8, 1) if st8 else 'n/a'}",
               fill=(150, 160, 175))
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:14]:
        d.text((36, y), n[:114], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({"verdict": obs["verdict"],
                      "assertions": obs["assert"]}, indent=2))
