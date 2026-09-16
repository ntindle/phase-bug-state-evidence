#!/usr/bin/env python3
"""Issue #7190: Sword of the Meek -- graveyard return does not attach to the
triggering 1/1 (v0.85.0 / protocol 72 validation run).

Oracle: "Whenever a 1/1 creature you control enters, you may return this card
from your graveyard to the battlefield, then attach it to that creature."

Reported: when Sword returns from the graveyard it does not correctly attach
to the 1/1 that caused its ability to trigger (absent or bound to the wrong
object). Triage acceptance criteria:
  - the returned Sword attaches to the exact triggering creature incarnation;
  - it does not attach to itself or a different 1/1;
  - if the triggering creature is illegal at resolution, Sword still returns
    but remains unattached.

PR #7557 ("Fix Sword of the Meek") merged 2026-08-21; matthewevans flagged
this as likely fixed-unreleased. This run verifies at runtime.

Three games, one scenario:
  Game 1 (accept):  P0 casts Sword T3; P1 Naturalizes it T4 (-> GY);
                    P0 casts Llanowar Elves T5 (-> trigger on stack);
                    both pass; P0 ACCEPTS the "you may return".
                    Expect: Sword on BF attached to the exact Elves (2/3).
  Game 2 (decline): same setup; P0 DECLINES the return (control branch).
                    Expect: Sword stays in GY.
  Game 3 (kill):    same setup; while the trigger is on the stack P1 Shocks
                    the Elves in response; P0 ACCEPTS the return.
                    Expect: Sword returns to BF UNATTACHED; Elves in GY.

Assertions:
  A1_setup_ok       accept: Sword reached P0 GY; Elves entered; trigger seen
  A2_choice_offered accept: the "you may return" choice was offered + answered
  A3_return_ok      accept: Sword on BF, controller P0
  A4_attach_correct accept: Sword attached_to == triggering Elves oid,
                    Elves is 2/3, Sword not attached to itself
  A5_decline_ok     decline: Sword remains in P0 GY after declining
  A6_unattached_ok  kill: Sword on BF unattached; triggering Elves in GY

Verdict: reproduced iff any assertion failed; not-reproduced iff all passed;
blocked iff some not-run.

Driver notes (protocol 72):
  - MulliganDecision answered as-is, gated on waiting_for.data.pending[]
    Declare entries per seat; mulligan (max 2) when the key card is missing.
  - PassPriority gated on my_priority (out-of-turn passes burn ticks).
  - Optional "you may" choice: decideOptionalEffect action-code surfaces,
    value true/false (Chaos Wand pattern).
  - attached_to may be a bare oid or a nested dict (recursive int search).
  - Engine auto-taps mana for casts; no payment prompt to drive.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260916-7190c"
ISSUE = 7190
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    # Recomputed 2026-09-16 against the on-disk pinned release (never copied
    # from a previous scenario file).
    "server_version": "v0.85.0",
    "build_commit": "cb58ef5",
    "protocol_version": 72,
    "server_binary_sha256": "263de0397ed915fc20ece1df1bf82d2ff6ef91f0c95e4d5856760783ca566a5f",
    "card_data_sha256": "a0b6e76bba31eace8cc6044164bc79e2b60b29244e4fdde63f21a27cbae67fed",
    "draft_pools_sha256": "163e6db8aa936f260e1d8b71d99a7db42caa0274479d80b0afb9e5785824b86e",
    "signature_verified": True,
    "server_run_id": "runs/20260916-7190c",
    "mode": "Full",
    "source": "this-run setup: sha256 recomputed on disk; minisign+manifest verified at pin time 2026-09-16",
}

SWORD = "sword of the meek"
ELVES = "llanowar elves"
FOREST = "forest"
MOUNTAIN = "mountain"
NAT = "naturalize"
SHOCK = "shock"

P0_DECK = [("Forest", 44), ("Sword of the Meek", 8), ("Llanowar Elves", 8)]
P1_DECK_AB = [("Forest", 44), ("Naturalize", 16)]
P1_DECK_KILL = [("Forest", 24), ("Mountain", 16), ("Naturalize", 8), ("Shock", 12)]

GAME_DEADLINE_S = 720   # 12 min per game
SETTLE_S = 120

ALL_ASS = {}
GAME_NOTES = []
SEEN_WF = []


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state utils
def obj_name(state, oid):
    o = (state.get("objects") or {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


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


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, dict):
        for k in ("value", "Fixed", "fixed"):
            if k in v:
                return num(v[k])
    return None


def attached_oid(o):
    a = o.get("attached_to")
    if a is None:
        return None
    if isinstance(a, int) and not isinstance(a, bool):
        return a
    if isinstance(a, dict):
        for k in ("object_id", "id", "oid"):
            if isinstance(a.get(k), int):
                return a[k]
    def rec(x):
        if isinstance(x, bool):
            return None
        if isinstance(x, int):
            return x
        if isinstance(x, dict):
            for v in x.values():
                r = rec(v)
                if r is not None:
                    return r
        if isinstance(x, list):
            for v in x:
                r = rec(v)
                if r is not None:
                    return r
        return None
    return rec(a)


def find_objs(state, name, zone=None, controller=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() != name:
            continue
        if zone and o.get("zone") != zone:
            continue
        if controller is not None and o.get("controller") != controller:
            continue
        out.append((int(oid), o))
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


def wf_type(state):
    return wf_of(state).get("type") or ""


def wf_player(state):
    return str((wf_of(state).get("data") or {}).get("player", ""))


def my_priority(state, pid):
    return wf_type(state) == "Priority" and state.get("priority_player") == pid


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def is_sword_trigger(state, entry):
    if not isinstance(entry, dict):
        return False
    kind = entry.get("kind") or {}
    if kind.get("type") != "TriggeredAbility":
        return False
    src = get_obj(state, entry.get("source_id"))
    nm = str(src.get("base_name") or src.get("name") or "").lower()
    if nm == SWORD:
        return True
    data = kind.get("data") or {}
    ab = data.get("ability") or {}
    desc = str(ab.get("description") or data.get("description") or "").lower()
    return "return this card from your graveyard" in desc


# ------------------------------------------------------------- interaction
def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def action_code(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            return (s.get("data") or {}).get("code")
    return None


def opp_candidates(opp):
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) or {}
    return rdata.get("candidates") or rdata.get("choices") or []


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
    say(f"[{tag}] submitting iid={str(iid)[:12]} choice={cid} ({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": c.name, "tag": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    await c.send_interaction(sub)
    return True


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a and a["data"] not in (None, {}):
        msg["data"] = a["data"]
    await c.send_action(msg)


def pick_optional(vi, want_accept):
    """Find the decideOptionalEffect choice; return (opp, choice) or (None, None)."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data", {}) or {}).get("choices", []) or []:
            codes = [(s.get("data") or {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if "decideOptionalEffect" not in codes:
                continue
            is_accept = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data") or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                txt = choice_text(ch).lower()
                if any(w in txt for w in ("return", "accept", "yes")):
                    is_accept = True
                elif any(w in txt for w in ("decline", "don't", "do not", "no")):
                    is_accept = False
            if is_accept == want_accept:
                return opp, ch
    return None, None

# ------------------------------------------------------------- common ticks
def mulligan_pending_for(state, pid):
    d = wf_of(state).get("data") or {}
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


async def do_mulligan(c, pid, tag, st, state, want_cards):
    if wf_type(state) != "MulliganDecision":
        return False
    if not mulligan_pending_for(state, pid):
        return False
    rev = (c.latest or {}).get("state_revision", -1)
    rev_key = f"mull_rev{pid}"
    if st.get(rev_key) == rev:
        return True
    st[rev_key] = rev
    hn = hand_lnames(state, pid)
    lands = sum(1 for n in hn if n in (FOREST, MOUNTAIN))
    mull_key = f"mulls{pid}"
    mulls = st.get(mull_key, 0)
    keep = (lands >= 2 and all(w in hn for w in want_cards)) or mulls >= 2
    if keep:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        say(f"[{tag}] mulligan: keep (lands={lands}, mulls={mulls})")
    else:
        st[mull_key] = mulls + 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[{tag}] mulligan #{mulls + 1} (lands={lands}, want={want_cards})")
    wire("mulligan", {"who": tag, "keep": keep, "n_lands": lands})
    return True


async def do_bottom(c, pid, tag, st, state, protect):
    if wf_type(state) != "MulliganDecision":
        return False
    acts = merged_actions(c.latest or {})
    sc = find_action(acts, "SelectCards")
    bot_key = f"bottomed{pid}"
    if not sc or st.get(bot_key):
        return False
    pending = (wf_of(state).get("data", {}) or {}).get("pending", []) or []
    count = 1
    for p in pending:
        if p.get("player") == pid and (p.get("phase") or {}).get("type") == "BottomCards":
            count = int((p.get("phase") or {}).get("count", 1))
    hand = hand_ids(state, pid)

    def bkey(oid):
        nm = lname(state, oid)
        if nm in protect:
            return 2
        if nm in (FOREST, MOUNTAIN):
            return 0
        return 1
    picks = sorted(hand, key=bkey)[:count]
    st[bot_key] = True
    await submit_as_is(c, {"type": "SelectCards",
                           "data": {"cards": [int(x) for x in picks]}})
    say(f"[{tag}] bottoming {count}: {[lname(state, x) for x in picks]}")
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def combat_tick(c, pid, tag, state, acts):
    wtype = wf_type(state)
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            import copy as _copy
            sub = _copy.deepcopy(da)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
        return True
    if wtype == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            import copy as _copy
            sub = _copy.deepcopy(da)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
        return True
    return False


async def discard_default(c, pid, tag, st, state, protect):
    if wf_type(state) != "DiscardToHandSize":
        return False
    if wf_player(state) != str(pid):
        return False
    vi = get_vi(c.latest or {})
    if not vi:
        return False
    n = (wf_of(state).get("data") or {}).get("count") or 1
    hand = hand_ids(state, pid)
    prio = sorted(hand, key=lambda o: 0 if lname(state, o) in (FOREST, MOUNTAIN)
                  else (2 if lname(state, o) in protect else 1))
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in st["answered"]:
            continue
        cands = opp_candidates(opp)
        if not cands:
            continue
        want = {str(x) for x in prio[:n]}
        picks = [ch.get("id") for ch in cands if str(ch.get("id")) in want][:n]
        if len(picks) < n:
            picks = [ch.get("id") for ch in cands[:n]]
        st["answered"].add(key)
        spec = ((opp.get("response", {}) or {}).get("data", {}) or {}).get("spec", {}) or {}
        rtype = spec.get("type") or "select"
        sub = {"interactionId": iid,
               "response": {"type": rtype, "data": {"choiceIds": picks}}}
        say(f"[{tag}] discarding {n} to hand size")
        wire("discard", {"who": tag, "picks": picks})
        await c.send_interaction(sub)
        return True
    return False


async def play_land_pref(c, acts, state, tag, prefer=None):
    plays = [a for a in acts if a["type"] == "PlayLand"]
    if not plays:
        return False
    if prefer:
        for a in plays:
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if oid is not None and lname(state, int(oid)) == prefer:
                await submit_as_is(c, a)
                return True
    await submit_as_is(c, plays[0])
    return True


def cast_spell_action(acts, state, name):
    for a in acts:
        if a.get("type") != "CastSpell":
            continue
        d = a.get("data") or {}
        oid = d.get("object_id") or a.get("_src_oid")
        if oid is not None and lname(state, int(oid)) == name:
            return a
    return None


async def common_tick(c, pid, tag, st, state, acts, protect):
    wtype = wf_type(state)
    if wtype == "MulliganDecision":
        if pid == 0:
            want = (SWORD,)
        elif st["mode"] == "kill":
            want = (NAT, SHOCK)
        else:
            want = (NAT,)
        if await do_mulligan(c, pid, tag, st, state, want):
            return True
        if await do_bottom(c, pid, tag, st, state, protect):
            return True
        return False
    if await pay_tick(c, acts, tag):
        return True
    if await combat_tick(c, pid, tag, state, acts):
        return True
    if await discard_default(c, pid, tag, st, state, protect):
        return True
    # Never pass priority while our own non-Priority interaction is pending;
    # the game-specific handler answers it. Hold here.
    vi = get_vi(c.latest or {})
    wp = wf_player(state)
    if vi and wtype != "Priority" and (wp == str(pid) or wp in ("", "None")):
        if wtype not in st["seen_wf_local"]:
            st["seen_wf_local"].append(wtype)
            say(f"[{tag}] holding on {wtype} (interaction pending)")
        wire("held_prompt", {"who": tag, "wf": wtype})
        return True
    return False

# ------------------------------------------------------------- game logic
def new_game_state(mode):
    return {
        "mode": mode, "stage": "SETUP",
        "sword_cast": False, "nat_cast": False, "elves_cast": False,
        "shock_cast": False, "shock_targeted": False,
        "elves_oid": None,          # BF oid of the triggering Elves
        "trigger_seen": False, "pre_exported": False,
        "choice_seen": False, "choice_answered": False,
        "post_exported": False, "settle_t0": None,
        "answered": set(), "seen_wf_local": [],
        "stack_hist": [],
        "ass": {},
    }


async def answer_optional(c, st, tag):
    """Answer P0's 'you may return' choice if offered. Returns True if acted."""
    vi = get_vi(c.latest or {})
    if not vi:
        return False
    want_accept = st["mode"] in ("accept", "kill")
    opp, ch = pick_optional(vi, want_accept)
    if not opp:
        return False
    iid = opp.get("interactionId")
    key = (tag, "optional", str(iid))
    if key in st["answered"]:
        return False
    st["answered"].add(key)
    st["choice_seen"] = True
    wire("optional_choice_opportunity",
         {"who": tag, "mode": st["mode"],
          "opportunity": {k: opp.get(k) for k in ("interactionId", "response")}})
    say(f"[{tag}] optional 'you may return' offered; answering "
        f"{'ACCEPT' if want_accept else 'DECLINE'}")
    await answer_vi(c, opp, ch, tag)
    st["choice_answered"] = True
    st["stage"] = "SETTLE"
    st["settle_t0"] = time.time()
    return True


async def answer_shock_target(c, st, tag, state):
    """Kill mode: answer P1's Shock TargetSelection with the Elves.

    Collects every available candidate carrying a target/object surface
    across all opportunities; submits the one resolving to the Elves
    (single-candidate prompt falls back to the lone target candidate).
    Action-menu choices carry 'action' surfaces, never target/object, so
    they can never be picked here.
    """
    if wf_type(state) != "TargetSelection":
        return False
    if wf_player(state) != "1":
        return False
    if not st["shock_cast"] or st["shock_targeted"]:
        return False
    vi = get_vi(c.latest or {})
    if not vi:
        return False
    elves = find_objs(state, ELVES, zone="Battlefield", controller=0)
    want_oid = st["elves_oid"] or (elves[0][0] if elves else None)
    pairs = []  # (opp, ch, surface_oid or None)
    for opp in vi.get("opportunities", []) or []:
        for ch in opp_candidates(opp):
            if ch.get("status", {}).get("type") != "available":
                continue
            surf_oid = None
            is_target = False
            for s in ch.get("surfaces", []) or []:
                if s.get("type") in ("target", "object"):
                    is_target = True
                    d = s.get("data") or {}
                    for k in ("object_id", "id", "oid"):
                        if isinstance(d.get(k), int):
                            surf_oid = d[k]
                            break
            if is_target:
                pairs.append((opp, ch, surf_oid))
    if not pairs:
        return False
    pick = None
    for opp, ch, surf_oid in pairs:
        if want_oid is not None and surf_oid == want_oid:
            pick = (opp, ch)
            break
    if pick is None and len(pairs) == 1:
        pick = (pairs[0][0], pairs[0][1])
    if pick is None:
        say(f"[{tag}] Shock target prompt: {len(pairs)} target candidates, "
            f"none matched Elves oid {want_oid}; holding")
        wire("shock_target_ambiguous",
             {"want_oid": want_oid,
              "candidates": [(str(o.get('interactionId'))[:12], surf)
                             for o, _, surf in pairs]})
        return False
    opp, ch = pick
    iid = opp.get("interactionId")
    key = (tag, "shock_target", str(iid))
    if key in st["answered"]:
        return False
    st["answered"].add(key)
    say(f"[{tag}] answering Shock target selection -> Elves oid {want_oid}")
    wire("shock_target", {"want_oid": want_oid,
                          "choice": choice_text(ch)[:80]})
    await answer_vi(c, opp, ch, tag)
    st["shock_targeted"] = True
    return True


async def p0_tick(c, st, tag="P0"):
    latest = c.latest or {}
    state, acts = latest.get("state", {}), merged_actions(latest)
    if not state:
        return
    # The optional "you may return" choice takes precedence over the hold.
    if await answer_optional(c, st, tag):
        return
    if await common_tick(c, 0, tag, st, state, acts, (SWORD, ELVES)):
        return
    turn = state.get("turn_number") or 0
    main = (state.get("active_player") == 0
            and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
    # Land drop every turn (retry each tick; no kept-flag).
    if main and my_priority(state, 0):
        if await play_land_pref(c, acts, state, tag):
            return
    # T3+: cast Sword of the Meek (once per game).
    if (not st["sword_cast"] and main and my_priority(state, 0) and turn >= 3
            and not stack_entries(state)):
        a = cast_spell_action(acts, state, SWORD)
        if a:
            say(f"[{tag}] casting Sword of the Meek (turn {turn})")
            wire("cast", {"who": tag, "card": SWORD, "action": a})
            await submit_as_is(c, a)
            st["sword_cast"] = True
            return
    # T5+ (T7+ in kill mode, giving P1 an untap between Naturalize and the
    # Elves trigger): cast Llanowar Elves once a Sword is in our graveyard.
    elves_turn = 7 if st["mode"] == "kill" else 5
    if (not st["elves_cast"] and main and my_priority(state, 0) and turn >= elves_turn
            and not stack_entries(state)):
        swords_gy = find_objs(state, SWORD, zone="Graveyard", controller=0)
        if swords_gy:
            a = cast_spell_action(acts, state, ELVES)
            if a:
                say(f"[{tag}] casting Llanowar Elves; Sword in GY "
                    f"(oids {[o for o, _ in swords_gy]})")
                wire("cast", {"who": tag, "card": ELVES, "action": a})
                await submit_as_is(c, a)
                st["elves_cast"] = True
                st["stage"] = "ELVES_IN_FLIGHT"
                return
    # Default: pass priority only when it is actually ours.
    if my_priority(state, 0):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return


async def p1_tick(c, st, tag="P1"):
    latest = c.latest or {}
    state, acts = latest.get("state", {}), merged_actions(latest)
    if not state:
        return
    if await answer_shock_target(c, st, tag, state):
        return
    if await common_tick(c, 1, tag, st, state, acts, (NAT, SHOCK)):
        return
    turn = state.get("turn_number") or 0
    main = (state.get("active_player") == 1
            and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
    if main and my_priority(state, 1):
        prefer = None
        if st["mode"] == "kill":
            hn = hand_lnames(state, 1)
            has_forest_bf = bool(find_objs(state, FOREST,
                                           zone="Battlefield",
                                           controller=1))
            has_mtn_bf = bool(find_objs(state, MOUNTAIN,
                                        zone="Battlefield",
                                        controller=1))
            # Forest first ({G} for T4 Naturalize), then Mountain ({R} for
            # T7 Shock). Elves is delayed to T7 so P1 untaps in between.
            if FOREST in hn and not has_forest_bf:
                prefer = FOREST
            elif MOUNTAIN in hn and not has_mtn_bf:
                prefer = MOUNTAIN
        if await play_land_pref(c, acts, state, tag, prefer=prefer):
            return
    # T4+: Naturalize the Sword (once per game).
    if (not st["nat_cast"] and main and my_priority(state, 1) and turn >= 4
            and not stack_entries(state)):
        swords_bf = find_objs(state, SWORD, zone="Battlefield", controller=0)
        if swords_bf:
            a = cast_spell_action(acts, state, NAT)
            if a:
                say(f"[{tag}] casting Naturalize targeting Sword "
                    f"(oids {[o for o, _ in swords_bf]})")
                wire("cast", {"who": tag, "card": NAT, "action": a})
                await submit_as_is(c, a)
                st["nat_cast"] = True
                return
    # Kill mode: Shock the Elves in response to the Sword trigger.
    if (st["mode"] == "kill" and not st["shock_cast"]
            and my_priority(state, 1)
            and any(is_sword_trigger(state, e) for e in stack_entries(state))):
        a = cast_spell_action(acts, state, SHOCK)
        if a:
            elves = find_objs(state, ELVES, zone="Battlefield", controller=0)
            say(f"[{tag}] casting Shock in response to Sword trigger; "
                f"Elves oids {[o for o, _ in elves]}")
            wire("cast", {"who": tag, "card": SHOCK, "action": a})
            await submit_as_is(c, a)
            st["shock_cast"] = True
            return
    if my_priority(state, 1):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return


async def observe(c, st, mode):
    """Sample the stack for the Sword trigger; export pre on first sighting."""
    latest = c.latest or {}
    state = latest.get("state", {})
    if not state:
        return
    for e in stack_entries(state):
        if is_sword_trigger(state, e):
            if not st["trigger_seen"]:
                st["trigger_seen"] = True
                eid = e.get("id")
                st["stack_hist"].append(
                    {"t": time.time(), "stack_id": eid,
                     "source_id": e.get("source_id")})
                say(f"[{mode}] Sword trigger observed on stack (id={eid})")
                wire("trigger_on_stack",
                     {"mode": mode, "entry": {k: e.get(k) for k in ("id", "kind", "source_id")}})
            if not st["pre_exported"]:
                say(f"[{mode}] exporting pre_{mode}.json (trigger on stack)")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre_{mode}.json", "w") as f:
                    f.write(pre)
                st["pre_exported"] = True
                st["stage"] = "TRIGGER"
            break
    # Record the triggering Elves incarnation once it is on the battlefield.
    if st["elves_oid"] is None:
        elves = find_objs(state, ELVES, zone="Battlefield", controller=0)
        if elves:
            st["elves_oid"] = elves[0][0]
            say(f"[{mode}] triggering Elves on BF: oid {st['elves_oid']}")
            wire("elves_bf", {"mode": mode, "oid": st["elves_oid"]})


async def check_settle(c, st, mode):
    """After the choice is answered, wait for a quiet board then export post."""
    if st["stage"] != "SETTLE" or st["post_exported"]:
        return False
    latest = c.latest or {}
    state = latest.get("state", {})
    if not state:
        return False
    if stack_entries(state):
        st["settle_t0"] = time.time()
        return False
    if get_vi(latest):
        st["settle_t0"] = time.time()
        return False
    if wf_type(state) not in ("Priority", ""):
        st["settle_t0"] = time.time()
        return False
    if time.time() - (st["settle_t0"] or time.time()) < 2.0:
        return False
    say(f"[{mode}] exporting post_{mode}.json (quiet board)")
    post = await c.export_state()
    with open(f"{EVDIR}/post_{mode}.json", "w") as f:
        f.write(post)
    st["post_exported"] = True
    st["stage"] = "DONE"
    return True

# ------------------------------------------------------------- assertions
def load_env(mode, which):
    p = f"{EVDIR}/{which}_{mode}.json"
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def evaluate(st, mode):
    a = st["ass"]
    pre = load_env(mode, "pre")
    post = load_env(mode, "post")
    pre_s = (pre or {}).get("state", {}) if pre else {}
    post_s = (post or {}).get("state", {}) if post else {}

    def S(sv, name, zone, controller=0):
        return find_objs(sv, name, zone=zone, controller=controller)

    if mode == "accept":
        # A1: setup reached the trigger with Sword in GY + Elves on BF
        if pre and st["trigger_seen"]:
            ok = (bool(S(pre_s, SWORD, "Graveyard")) and bool(S(pre_s, ELVES, "Battlefield")))
            a["A1_setup_ok"] = "passed" if ok else "failed"
            if not ok:
                st_note(st, f"A1: pre state missing Sword-in-GY or Elves-on-BF")
        else:
            a["A1_setup_ok"] = "not-run"
            st_note(st, "A1: no pre-export or trigger never observed")
        # A2: the "you may return" choice was offered and answered
        if st["choice_seen"] and st["choice_answered"]:
            a["A2_choice_offered"] = "passed"
        elif st["choice_seen"]:
            a["A2_choice_offered"] = "failed"
            st_note(st, "A2: choice offered but not answered")
        else:
            a["A2_choice_offered"] = "not-run"
            st_note(st, "A2: optional choice never observed")
        # A3/A4: post state
        if post:
            swords_bf = S(post_s, SWORD, "Battlefield")
            a["A3_return_ok"] = "passed" if swords_bf else "failed"
            if swords_bf:
                sw_oid, sw = swords_bf[0]
                att = attached_oid(sw)
                elves = get_obj(post_s, st["elves_oid"]) if st["elves_oid"] else {}
                e_pt = (num(elves.get("power")), num(elves.get("toughness")))
                st_note(st, f"A4: sword oid {sw_oid} attached_to={att}; "
                            f"triggering Elves oid {st['elves_oid']} pt={e_pt}")
                if (att is not None and att == st["elves_oid"]
                        and att != sw_oid and e_pt == (2, 3)):
                    a["A4_attach_correct"] = "passed"
                else:
                    a["A4_attach_correct"] = "failed"
            else:
                a["A4_attach_correct"] = "failed"
                st_note(st, "A4: Sword not on battlefield; attach not evaluable")
        else:
            a["A3_return_ok"] = a["A4_attach_correct"] = "not-run"
            st_note(st, "A3/A4: no post-export")
    elif mode == "decline":
        if post:
            swords_gy = S(post_s, SWORD, "Graveyard")
            swords_bf = S(post_s, SWORD, "Battlefield")
            if swords_gy and not swords_bf:
                a["A5_decline_ok"] = "passed"
            else:
                a["A5_decline_ok"] = "failed"
            st_note(st, f"A5: post GY swords={[o for o, _ in swords_gy]} "
                        f"BF swords={[o for o, _ in swords_bf]}")
        else:
            a["A5_decline_ok"] = "not-run"
            st_note(st, "A5: no post-export")
    elif mode == "kill":
        if post:
            swords_bf = S(post_s, SWORD, "Battlefield")
            elves_gy = S(post_s, ELVES, "Graveyard")
            if swords_bf:
                sw_oid, sw = swords_bf[0]
                att = attached_oid(sw)
                st_note(st, f"A6: sword oid {sw_oid} attached_to={att}; "
                            f"Elves in GY={[o for o, _ in elves_gy]}")
                if att is None and elves_gy:
                    a["A6_unattached_ok"] = "passed"
                else:
                    a["A6_unattached_ok"] = "failed"
            else:
                a["A6_unattached_ok"] = "failed"
                st_note(st, "A6: Sword not on battlefield after kill-leg")
        else:
            a["A6_unattached_ok"] = "not-run"
            st_note(st, "A6: no post-export")
    for k, v in a.items():
        ALL_ASS[k] = v


def st_note(st, msg):
    st.setdefault("notes", []).append(msg)
    GAME_NOTES.append(f"[{st['mode']}] {msg}")


# ------------------------------------------------------------- per-game run
async def run_game(mode, p1_deck):
    st = new_game_state(mode)
    say(f"===== game start: mode={mode} =====")
    wire("game_start", {"mode": mode})
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    t0 = time.time()
    try:
        await p0.create(deck(*P0_DECK), player_count=2)
        await p1.join(p0.game_code, deck(*p1_deck))
        await asyncio.sleep(2.0)
        st["game_code"] = p0.game_code
        say(f"game {p0.game_code} mode={mode}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
        wire("game_setup", {"mode": mode, "game_code": p0.game_code,
                            "p0_seat": p0.player_id, "p1_seat": p1.player_id})

        last, last_tick_at, last_diag = {}, {}, 0.0
        while True:
            await asyncio.sleep(0.15)
            now = time.time()
            elapsed = now - t0
            if elapsed > GAME_DEADLINE_S and st["stage"] != "DONE":
                st_note(st, f"game deadline ({GAME_DEADLINE_S}s) hit at stage {st['stage']}")
                for k in ("A1_setup_ok", "A2_choice_offered", "A3_return_ok",
                          "A4_attach_correct", "A5_decline_ok", "A6_unattached_ok"):
                    st["ass"].setdefault(k, "not-run")
                break

            for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
                latest = c.latest
                if not latest or not latest.get("state"):
                    continue
                same_rev = (c.revision == last.get(tag))
                stale = now - last_tick_at.get(tag, 0) > 5
                if same_rev and not stale:
                    continue
                last[tag] = c.revision
                last_tick_at[tag] = now
                try:
                    await tick(c, st, tag)
                except Exception as e:
                    say(f"tick error {tag}: {e}")
                    wire("tick_error", {"who": tag, "err": str(e)})

            if p0.latest and p0.latest.get("state"):
                try:
                    await observe(p0, st, mode)
                    if st["stage"] == "SETTLE":
                        if await check_settle(p0, st, mode):
                            break
                except Exception as e:
                    say(f"observe error: {e}")
                    wire("observe_error", {"err": str(e)})

            s = (p0.latest or {}).get("state", {})
            if (s.get("turn_number") or 0) > 200 and st["stage"] == "SETUP":
                st_note(st, "turn 200 hit in SETUP; finishing game")
                for k in ("A1_setup_ok", "A2_choice_offered", "A3_return_ok",
                          "A4_attach_correct", "A5_decline_ok", "A6_unattached_ok"):
                    st["ass"].setdefault(k, "not-run")
                break
            if now - last_diag > 60 and s:
                last_diag = now
                say(f"DIAG[{mode}] turn={s.get('turn_number')} active={s.get('active_player')} "
                    f"phase={s.get('phase')} wf={wf_type(s)} pp={s.get('priority_player')} "
                    f"stage={st['stage']} stack={len(stack_entries(s))} "
                    f"sword_cast={st['sword_cast']} nat={st['nat_cast']} "
                    f"elves={st['elves_cast']} trig={st['trigger_seen']} "
                    f"choice={st['choice_answered']} shock={st['shock_cast']}")

        # Fallback exports so the evidence dir stays complete.
        for which, flag in (("pre", "pre_exported"), ("post", "post_exported")):
            if not st[flag]:
                try:
                    data = await p0.export_state()
                    with open(f"{EVDIR}/{which}_{mode}.json", "w") as f:
                        f.write(data)
                    st[flag] = True
                    st_note(st, f"{which}_{mode}.json exported at fallback (NOT the contract state)")
                except Exception as e:
                    st_note(st, f"{which}_{mode} export failed: {e}")
        evaluate(st, mode)
        say(f"game {mode} done: " +
            ", ".join(f"{k}={v}" for k, v in st["ass"].items()))
        for w in st["seen_wf_local"]:
            if w not in SEEN_WF:
                SEEN_WF.append(w)
    finally:
        for c in (p0, p1):
            try:
                await c.close()
            except Exception:
                pass
    return st


# ------------------------------------------------------------- finalization
def finalize_assertions():
    for k in ("A1_setup_ok", "A2_choice_offered", "A3_return_ok",
              "A4_attach_correct", "A5_decline_ok", "A6_unattached_ok"):
        ALL_ASS.setdefault(k, "not-run")
    vals = list(ALL_ASS.values())
    if all(v == "passed" for v in vals):
        verdict = "not-reproduced"
    elif any(v == "failed" for v in vals):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    return ALL_ASS, verdict


def write_manifest(files):
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    # Convention: the manifest lists every evidence file except itself,
    # written after all other files are final.
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def capture_server_excerpts(game_codes):
    try:
        logp = f"{BACKFILL}/{SERVER_IDENTITY['server_run_id']}/server.log"
        if not os.path.exists(logp):
            say(f"no server.log at {logp}; skipping excerpts")
            return
        out = []
        with open(logp, errors="replace") as f:
            for line in f:
                if any(gc in line for gc in game_codes if gc):
                    out.append(line)
        out = out[-300:]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(out)
        say(f"wrote server_excerpts.log ({len(out)} lines)")
    except Exception as e:
        say(f"server excerpts failed: {e}")


def write_parse_files():
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.85.0/data/card-data.json"))
        card = cd.get("sword of the meek", {})
        with open(f"{EVDIR}/parse_sword.json", "w") as f:
            json.dump({"name": card.get("name"),
                       "oracle_text": card.get("oracle_text"),
                       "triggers": card.get("triggers"),
                       "static_abilities": card.get("static_abilities")},
                      f, indent=2)
    except Exception as e:
        GAME_NOTES.append(f"parse json failed: {e}")


async def main():
    t_start = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start))
    say(f"server identity: v0.85.0 (cb58ef5) protocol 72, mode Full; "
        f"live server {SERVER_IDENTITY['server_run_id']}")
    game_codes = []
    # Accept leg first (the reported path), then decline control, then kill leg.
    for mode, p1_deck in (("accept", P1_DECK_AB),
                          ("decline", P1_DECK_AB),
                          ("kill", P1_DECK_KILL)):
        st = await run_game(mode, p1_deck)
        if st.get("game_code"):
            game_codes.append(st["game_code"])

    # Primary pre/post pair = the accept leg (reported path).
    import shutil
    for which in ("pre", "post"):
        src = f"{EVDIR}/{which}_accept.json"
        if os.path.exists(src):
            shutil.copyfile(src, f"{EVDIR}/{which}.json")

    a, verdict = finalize_assertions()
    write_parse_files()
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": started_at,
        "duration_s": round(time.time() - t_start, 1),
        "server": {
            "server_version": SERVER_IDENTITY["server_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "mode": SERVER_IDENTITY["mode"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "observed_at": "2026-09-16",
            "source": SERVER_IDENTITY["source"],
        },
        "server_run_dir": SERVER_IDENTITY["server_run_id"],
        "game_codes": game_codes,
        "driver": {"protocol_advertised": 72, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7190.py", "rb").read()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1_accept_decline": P1_DECK_AB,
                  "P1_kill": P1_DECK_KILL},
        "assertions": a,
        "notes": GAME_NOTES,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Restoration: the prebuilt phase-server has no standalone "
            "state-restore facility; pre/post JSON are authoritative exports "
            "restorable only via full game replay, not direct load.",
            "Dense playsets (8x Sword/Elves, 16x Naturalize) are a "
            "test-harness convenience; the engine accepts >4-of for custom games.",
        ],
        "setup_line": ("P0: 44 Forest + 8 Sword of the Meek + 8 Llanowar Elves; "
                       "P1: 44 Forest + 16 Naturalize (accept/decline) or "
                       "24 Forest + 16 Mountain + 8 Naturalize + 12 Shock (kill); "
                       "kill leg delays Elves to T7 so P1 untaps between "
                       "T4 Naturalize and T7 Shock"),
        "contract_line": ("Sword cast T3, Naturalized T4 (-> GY), Elves T5 "
                          "(-> trigger); accept/decline the 'you may return'; "
                          "kill leg Shocks the Elves in response"),
        "stats": {
            "seen_wf_types": SEEN_WF,
            "game_codes": game_codes,
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"wrote run.json verdict={verdict}")

    # Render AFTER run.json is final (render reads run.json + pre/post).
    import subprocess as _sp
    r = _sp.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                 EVDIR, str(ISSUE),
                 "Sword of the Meek -- graveyard return does not attach to the triggering 1/1"],
                capture_output=True, text=True)
    say(r.stdout.strip() or "(render ok)")
    if r.returncode != 0:
        say(f"render_summary failed: {r.stderr[:500]}")
        raise RuntimeError("render_summary failed")

    await capture_server_excerpts(game_codes)
    RUNLOG.close()
    WIRE.close()
    shutil.copyfile(f"{BACKFILL}/driver/scenario_7190.py",
                    f"{EVDIR}/scenario_7190.py")
    print("copied scenario_7190.py into EVDIR", flush=True)

    files = ["pre.json", "post.json",
             "pre_accept.json", "post_accept.json",
             "pre_decline.json", "post_decline.json",
             "pre_kill.json", "post_kill.json",
             "run.json", "scenario_7190.py", "wire_log.jsonl",
             "scenario_run.log", "summary.png", "parse_sword.json",
             "server_excerpts.log"]
    write_manifest(files)  # AFTER all say() logging is done

    # Validate.
    say("validating evidence dir...")
    for fn in ("pre.json", "post.json"):
        env = json.load(open(f"{EVDIR}/{fn}"))
        assert "state" in env, f"{fn} missing state envelope"
        say(f"  {fn}: parses, state envelope ok")
    for fn in ("pre_decline.json", "post_decline.json",
               "pre_kill.json", "post_kill.json"):
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            env = json.load(open(p))
            assert "state" in env, f"{fn} missing state envelope"
    run2 = json.load(open(f"{EVDIR}/run.json"))
    assert run2.get("verdict") in ("reproduced", "not-reproduced", "blocked")
    say(f"  run.json: parses, verdict={run2['verdict']}, "
        f"{len(run2['assertions'])} assertions")
    manifest = {}
    for line in open(f"{EVDIR}/manifest.sha256"):
        line = line.strip()
        if not line:
            continue
        h, fn = line.split("  ", 1)
        manifest[fn] = h
    for fn, h in manifest.items():
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        assert actual == h, f"hash mismatch for {fn}"
    say(f"  manifest: {len(manifest)} files, all hashes match")
    from PIL import Image
    im = Image.open(f"{EVDIR}/summary.png")
    im.verify()
    say("  summary.png: PIL verify ok")
    print(f"DONE issue={ISSUE} run={RUN_ID} verdict={verdict}", flush=True)


if __name__ == "__main__":
    print(asyncio.run(main()))
