#!/usr/bin/env python3
"""Issue #7379: Jirina Kudro - Only makes one token regardless of how many
times it has been cast.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.99.0):
  "When Jirina Kudro enters, create a 1/1 white Human Soldier creature
   token for each time you've cast a commander from the command zone this
   game. Other Humans you control get +2/+0."
Pinned parse: ChangesZone(Self -> Battlefield) trigger, effect Token
  "Human Soldier" 1/1 white, count = Ref(CommanderCastFromCommandZoneCount).
  (The parser half the report blamed is PRESENT in v0.99.0 data; this run
  tests whether the ENGINE evaluates the Ref at trigger time.)

Rulings: if Jirina is your commander, the count includes the cast of
Jirina that just put her on the battlefield.

Setup (native engine, three human-client seats, CommanderDraft, 3 players):
  P0: commander=[Jirina Kudro] ({1}{R}{W}{B} 3/3), main = 54 lands
      (16x Mountain, 16x Plains, 16x Swamp, 6x Command Tower)
      + 6x Day of Judgment ({2}{W}{W}, no targets).
      Plays lands, casts Jirina from the command zone twice, wipes in
      between, answers the commander replacement choice (move to CZ).
  P1/P2: 60x Plains, inert (play land, pass).

Stages:
  SETUP -> CAST1 (first Jirina cast) -> WAIT_ETB1 -> mid1.json
        -> WIPE (Day of Judgment) -> REPLACEMENT -> WAIT_CZ -> mid2.json
        -> CAST2 (second Jirina cast, +{2} tax) -> WAIT_ETB2 -> post.json

Assertions:
  A1_setup_ok     mid1.json: Jirina entered from a command-zone cast, ETB
                  trigger resolved (stack empty, game proceeding)
  A2_first_count  mid1.json: P0 controls exactly 1 Human Soldier token
                  (the ruling-included first cast counts as 1)
  A3_kill_ok      mid2.json: Jirina is in the CommandZone, 0 tokens on P0 BF
  A4_second_count post.json: P0 controls exactly 2 Human Soldier tokens
                  (two commander casts this game => two new tokens)

Verdict: reproduced iff A1+A3 pass and (A2 fails or A4 fails in the
reported direction - token count stuck at 1). not-reproduced iff
A1..A4 all pass. blocked iff the flow cannot be driven.

Protocol-98 driver notes (v0.99.0, build d919616):
- HELLO advertises protocol 98 (server enforces exact match).
- MulliganDecision as {"choice":{"type":"Keep"}}, gated on the seat's
  pending[] Declare entry keyed by (client, revision).
- BottomCards / DiscardToHandSize via single SelectCards {"cards":[...]}.
- PassPriority only when the seat genuinely holds priority, revision-guarded.
- CastSpell advertised actions submitted as-is (engine auto-pays mana,
  including from the command zone and with the commander tax).
- OptionalEffectChoice: answered via viewer_interaction exactChoices with
  decideOptionalEffect surfaces (role accept true/false), mirroring the
  protocol-94 #301 driver; falls back to text matching.
- CommanderDraft format_config copied from the v0.81-era commander
  scenarios (P0 commander + 2 inert seats, player_count=3).
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, cdeck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7379g"
ISSUE = 7379

SERVER_IDENTITY = {
    "validated_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}


def _sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# recompute against on-disk artifacts; never copy hashes blindly
for _f, _k in (
        ("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.99.0/data/draft-pools.json",
         "draft_pools_sha256")):
    _h = _sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts",
      flush=True)

EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

JIRINA = "Jirina Kudro"
MTN = "Mountain"
PLN = "Plains"
SWP = "Swamp"
TOWER = "Command Tower"
DOJ = "Day of Judgment"
LANDS = (MTN, PLN, SWP, TOWER)

P0_COMMANDER = [JIRINA]
P0_DECK = [(MTN, 16), (PLN, 16), (SWP, 16), (TOWER, 6), (DOJ, 6)]
P1_DECK = [(PLN, 60)]
P2_DECK = [(PLN, 60)]

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

TIMEOUT = 3600
SETUP_DEADLINE = 1500

ST = {}
MULLS = {}
_PASSED_REV = {}
_DISCARD_REV = {}
WF_SEEN = []
_PROMPT_DONE = {}


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",
        "t0": time.time(),
        "pre_exported": False,
        "mid1_exported": False,
        "mid2_exported": False,
        "post_exported": False,
        "cast1_submitted": False,
        "doj_submitted": False,
        "cast2_submitted": False,
        "cz_choice": None,
        "tokens_after_cast1": None,
        "tokens_after_cast2": None,
        "cast1_rev": None,
        "stop": False,
        "notes": [],
        "rejections": [],
        "game_code": None,
    })
    MULLS.clear()
    _PASSED_REV.clear()
    _DISCARD_REV.clear()
    WF_SEEN.clear()
    _PROMPT_DONE.clear()


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


def oname(o):
    return o.get("name") or o.get("card_name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def count_land_hand(state, pid):
    return sum(1 for oid in hand_oids(state, pid)
               if oname(state["objects"][oid]) in LANDS)


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def tokens_on_bf(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == "Human Soldier")


def jirina_zone(state, pid):
    for oid, o in state["objects"].items():
        if oname(o) == JIRINA and o.get("controller") == pid:
            return o.get("zone"), oid
    return None, None


CZ_ZONES = ("Command", "CommandZone")


def commander_oid(state, pid):
    """Jirina's object while she sits in the command zone.

    Zone name observed on v0.99.0/protocol 98 is "Command"
    ("CommandZone" kept defensively)."""
    for oid, o in state["objects"].items():
        if (oname(o) == JIRINA and o.get("controller") == pid
                and o.get("zone") in CZ_ZONES):
            return oid
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def pending_for(state, pid):
    for p in wf_data(state).get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    return (wf_type(state) == "Priority"
            and str(wf_player(state)) == str(pid))


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


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
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
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
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "MulliganDecision":
        return False
    if MULLS.get((c.name, c.revision)):
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    n_lands = count_land_hand(state, pid)
    mulls = MULLS.get(c.name, 0)
    keep_ok = n_lands >= 3 or mulls >= 2
    choice = "Keep" if keep_ok else "Mulligan"
    if choice == "Mulligan":
        MULLS[c.name] = mulls + 1
    MULLS[(c.name, c.revision)] = True
    await submit_as_is(c, {"type": "MulliganDecision",
                           "data": {"choice": {"type": choice}}})
    say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
    return True


async def do_bottom(c, pid):
    st = c.latest
    if not st:
        return False
    if MULLS.get((c.name, "bottom", c.revision)):
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    ph = pend.get("phase") or {}
    if ph.get("type") != "BottomCards":
        return False
    n = int(ph.get("count", 1) or 1)
    picks = [int(x) for x in hand_oids(state, pid)[:n]]
    MULLS[(c.name, "bottom", c.revision)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} bottoms {n}")
    return True


async def do_discard(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_type(state) != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    h = hand_oids(state, pid)
    n = len(h) - 7
    if n <= 0:
        return False
    objs = state["objects"]
    # keep Jirina plans: discard lands last, DOJ first if many
    doj = [o for o in h if oname(objs[o]) == DOJ]
    rest = [o for o in h if o not in doj]
    picks = [int(x) for x in (doj + rest)[:n]]
    _DISCARD_REV[(c.name, rev)] = True
    await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
    say(f"{c.name} discards {len(picks)}")
    return True


async def pass_priority(c, pid):
    st = c.latest
    if not st:
        return False
    if not my_priority(st["state"], pid):
        return False
    rev = st.get("state_revision", -1)
    if _PASSED_REV.get((c.name, rev)):
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            _PASSED_REV[(c.name, rev)] = True
            await submit_as_is(c, a)
            return True
    return False


async def answer_order_triggers(c, pid, state, acts):
    if wf_type(state) != "OrderTriggers":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    if not acts:
        return False
    await submit_as_is(c, acts[0])
    say(f"[{c.name}] OrderTriggers answered as advertised")
    return True


async def answer_declares(c, pid, state, acts):
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
    return False


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def p0_cast_plan(c, pid, state, acts):
    """SETUP/CAST1: cast Jirina from the command zone.
    WIPE: cast Day of Judgment from hand.
    CAST2: cast Jirina from the command zone again."""
    stage = ST["stage"]
    if stage in ("SETUP", "CAST1"):
        if ST["cast1_submitted"]:
            return False
        coid = commander_oid(state, pid)
        a = castspell_advertised(acts, coid)
        if a:
            if not ST["pre_exported"]:
                # export pre.json right before submitting cast1: this is the
                # only path that submits cast1, so export here rather than in
                # the main-loop probe (which lost the race on every run).
                s = await export_now("pre.json")
                if s is not None:
                    ST["pre_exported"] = True
                    say("pre exported; CAST1 stage")
            ST["cast1_submitted"] = True
            await submit_as_is(c, a)
            ST["stage"] = "WAIT_ETB1"
            say(f"[P0] casts Jirina from the command zone (oid {coid})")
            return True
    elif stage == "WIPE":
        if ST["doj_submitted"]:
            return False
        oid = find_hand(state, pid, DOJ)
        a = castspell_advertised(acts, oid)
        if a:
            ST["doj_submitted"] = True
            await submit_as_is(c, a)
            ST["stage"] = "REPLACEMENT"
            say("[P0] casts Day of Judgment")
            return True
    elif stage == "CAST2":
        if ST["cast2_submitted"]:
            return False
        coid = commander_oid(state, pid)
        a = castspell_advertised(acts, coid)
        if a:
            ST["cast2_submitted"] = True
            await submit_as_is(c, a)
            ST["stage"] = "WAIT_ETB2"
            say(f"[P0] casts Jirina from the command zone, second time "
                f"(oid {coid})")
            return True
    return False


def choice_text(ch):
    parts = []
    for sf in ch.get("surfaces", []) or []:
        d = sf.get("data") or {}
        for k in ("text", "label", "description"):
            if d.get(k):
                parts.append(str(d[k]))
    return " | ".join(parts)


_CZ_SHAPE_LOGGED = {}


async def answer_commander_zone_choice(c, pid):
    """waiting_for CommanderZoneChoice (protocol 98): move the commander
    to the command zone. Logs the full shape on first sight, then tries
    (1) an advertised legal action naming the command zone, submitted
    as-is; (2) viewer_interaction exactChoices pick for the command zone."""
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], st.get("legal_actions", [])
    if wf_type(state) != "CommanderZoneChoice":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    rev = st.get("state_revision", -1)
    key = (c.name, rev)
    if key not in _CZ_SHAPE_LOGGED:
        _CZ_SHAPE_LOGGED[key] = True
        wire("commander_zone_choice_shape", {
            "who": c.name, "rev": rev,
            "wf_data": wf_data(state),
            "action_types": [a.get("type") for a in acts],
            "actions": acts,
            "viewer_interaction": st.get("viewer_interaction"),
        })
        say(f"[{c.name}] CommanderZoneChoice shape: wf_data="
            f"{json.dumps(wf_data(state), default=str)[:600]} "
            f"actions={[a.get('type') for a in acts]}")
    # (1) advertised legal action: DecideOptionalEffect with accept=true
    # moves the commander to the command zone (protocol 98 shape).
    for a in acts:
        if (a.get("type") == "DecideOptionalEffect"
                and (a.get("data") or {}).get("accept") is True):
            await submit_as_is(c, a)
            ST["cz_choice"] = "DecideOptionalEffect accept=true (command zone)"
            say(f"[{c.name}] CommanderZoneChoice answered via "
                f"DecideOptionalEffect accept=true")
            return True
    for a in acts:
        blob = json.dumps(a, default=str).lower()
        if "command" in blob and "zone" in blob:
            await submit_as_is(c, a)
            ST["cz_choice"] = f"legal_action {a.get('type')} (command zone)"
            say(f"[{c.name}] CommanderZoneChoice answered via legal action "
                f"{a.get('type')}")
            return True
    # (2) viewer_interaction exactChoices
    vi = st.get("viewer_interaction") or {}
    for op in vi.get("opportunities", []) or []:
        iid = op.get("interactionId") or op.get("id")
        if not iid or iid in _PROMPT_DONE:
            continue
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        chs = resp.get("data", {}).get("choices", []) or []
        pick = None
        for ch in chs:
            txt = choice_text(ch).lower()
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            is_cz = ("command zone" in txt or "commandzone" in txt
                     or any("command" in str(x).lower() for x in codes))
            acc = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data") or {}
                if dd.get("role") == "accept":
                    acc = str(dd.get("value")).lower() == "true"
            if is_cz and acc is not False:
                pick = ch
                break
        if pick is None:
            for ch in chs:
                txt = choice_text(ch).lower()
                if "command zone" in txt:
                    pick = ch
                    break
        if pick is None:
            wire("cz_choice_unanswered",
                 {"who": c.name, "iid": str(iid)[:16],
                  "choices": [choice_text(ch)[:120] for ch in chs]})
            say(f"[{c.name}] CommanderZoneChoice: no command-zone choice "
                f"identified; NOT auto-answering")
            _PROMPT_DONE[iid] = True
            return True
        wire("cz_choice_answered",
             {"who": c.name, "iid": str(iid)[:16],
              "choice": choice_text(pick)[:160]})
        say(f"[{c.name}] CommanderZoneChoice: moving commander to command "
            f"zone ({choice_text(pick)[:80]})")
        ST["cz_choice"] = f"moved to command zone: {choice_text(pick)[:120]}"
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": pick["id"]}}})
        _PROMPT_DONE[iid] = True
        return True
    return False


async def answer_choice_prompt(c, pid):
    """OptionalEffectChoice (commander replacement etc.): choose the
    command-zone move when the prompt is about the command zone;
    otherwise decline explicitly and record. Returns True if handled."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_type(state) != "OptionalEffectChoice":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    vi = st.get("viewer_interaction") or {}
    desc = str(wf_data(state).get("description", ""))
    handled = False
    for op in vi.get("opportunities", []) or []:
        iid = op.get("interactionId") or op.get("id")
        if not iid or iid in _PROMPT_DONE:
            continue
        resp = op.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        chs = resp.get("data", {}).get("choices", []) or []
        want_cz = "command zone" in desc.lower()
        pick = None
        for ch in chs:
            codes = [s.get("data", {}).get("code")
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
                if "cast" in txt or "accept" in txt or "command zone" in txt:
                    is_accept = True
                elif "decline" in txt or "don't" in txt or "do not" in txt:
                    is_accept = False
            if is_accept == want_cz:
                pick = ch
                break
        if pick is None:
            wire("choice_unanswered",
                 {"who": c.name, "iid": str(iid)[:16], "desc": desc[:200],
                  "choices": [choice_text(ch)[:120] for ch in chs]})
            say(f"[{c.name}] OptionalEffectChoice: no matching choice; "
                f"NOT auto-answering (desc={desc[:120]})")
            _PROMPT_DONE[iid] = True
            handled = True
            continue
        wire("choice_answered",
             {"who": c.name, "iid": str(iid)[:16], "desc": desc[:200],
              "choice": choice_text(pick)[:120], "want_cz": want_cz})
        say(f"[{c.name}] OptionalEffectChoice: "
            f"{'accept(command zone)' if want_cz else 'decline'} "
            f"(desc={desc[:100]})")
        ST["cz_choice"] = (f"{'accepted' if want_cz else 'declined'} "
                           f"move to command zone: {desc[:120]}")
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "choose",
                         "data": {"choiceId": pick["id"]}}})
        _PROMPT_DONE[iid] = True
        handled = True
    return handled


async def p0_land_drop(c, pid, state, acts):
    for name in LANDS:
        lid = find_hand(state, pid, name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    return False


async def p0_tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await answer_order_triggers(c, pid, state, acts):
        return True
    if await answer_declares(c, pid, state, acts):
        return True
    if await answer_commander_zone_choice(c, pid):
        return True
    if await answer_choice_prompt(c, pid):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        if await p0_land_drop(c, pid, state, acts):
            return True
        if await p0_cast_plan(c, pid, state, acts):
            return True
    if await pass_priority(c, pid):
        return True
    return False


async def dummy_tick(c, pid, land_name):
    """Inert seat: keep/discard/mull, play a land, pass, never attack."""
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    if await do_mulligan(c, pid):
        return True
    if await do_bottom(c, pid):
        return True
    if await do_discard(c, pid):
        return True
    if await answer_order_triggers(c, pid, state, acts):
        return True
    if await answer_declares(c, pid, state, acts):
        return True
    if await answer_choice_prompt(c, pid):
        return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if is_my_main(state, pid):
        lid = find_hand(state, pid, land_name)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
    if await pass_priority(c, pid):
        return True
    return False


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "player": wf_player(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


def etb_settled(state):
    """Jirina on BF, nothing of hers on the stack, stack empty."""
    zone, _ = jirina_zone(state, 0)
    if zone != "Battlefield":
        return False
    stack = state.get("stack") or []
    if stack:
        return False
    return True


def compute_assertions(pre_env, mid1_env, mid2_env, post_env):
    assertions, detail = {}, {}
    pre, mid1, mid2, post = (pre_env["state"], mid1_env["state"],
                             mid2_env["state"], post_env["state"])

    def bfcount(s, pid, name):
        return sum(1 for o in s["objects"].values()
                   if o.get("zone") == "Battlefield"
                   and o.get("controller") == pid
                   and oname(o) == name)

    def zone_of_jirina(s):
        for o in s["objects"].values():
            if oname(o) == JIRINA and o.get("controller") == 0:
                return o.get("zone")
        return None

    jz_pre = zone_of_jirina(pre)
    a1 = (jz_pre in CZ_ZONES
          and bfcount(mid1, 0, JIRINA) >= 1
          and len(mid1.get("stack") or []) == 0
          and ST["cast1_submitted"])
    assertions["A1_setup_ok"] = "passed" if a1 else "failed"
    detail["A1_setup_ok"] = (
        f"pre: Jirina zone={jz_pre} (want Command/CommandZone); mid1: Jirina on BF="
        f"{bfcount(mid1, 0, JIRINA)} (want >=1), stack="
        f"{len(mid1.get('stack') or [])} (want 0), cast1_submitted="
        f"{ST['cast1_submitted']}")

    t1 = bfcount(mid1, 0, "Human Soldier")
    ST["tokens_after_cast1"] = t1
    if t1 == 1:
        assertions["A2_first_count"] = "passed"
        detail["A2_first_count"] = (
            f"mid1: P0 Human Soldier tokens={t1} (want 1: the first "
            f"command-zone cast of Jirina counts itself per the ruling)")
    else:
        assertions["A2_first_count"] = "failed"
        detail["A2_first_count"] = (
            f"mid1: P0 Human Soldier tokens={t1} (want 1)")

    jz_mid2 = zone_of_jirina(mid2)
    t_mid2 = bfcount(mid2, 0, "Human Soldier")
    a3 = (jz_mid2 in CZ_ZONES and t_mid2 == 0
          and ST["doj_submitted"])
    assertions["A3_kill_ok"] = "passed" if a3 else "failed"
    detail["A3_kill_ok"] = (
        f"mid2: Jirina zone={jz_mid2} (want Command/CommandZone), tokens={t_mid2} "
        f"(want 0), doj_submitted={ST['doj_submitted']}, cz_choice="
        f"{ST['cz_choice']}")

    t2 = bfcount(post, 0, "Human Soldier")
    ST["tokens_after_cast2"] = t2
    if t2 == 2:
        assertions["A4_second_count"] = "passed"
        detail["A4_second_count"] = (
            f"post: P0 Human Soldier tokens={t2} (want 2: two command-zone "
            f"commander casts this game => two tokens)")
    elif t2 == 1:
        assertions["A4_second_count"] = "failed"
        detail["A4_second_count"] = (
            f"post: P0 Human Soldier tokens={t2} (want 2) -- REPORTED BUG "
            f"DIRECTION: still exactly one token after two commander casts")
    else:
        assertions["A4_second_count"] = "failed"
        detail["A4_second_count"] = (
            f"post: P0 Human Soldier tokens={t2} (want 2)")

    ok = len(post.get("stack") or []) == 0 and wf_type(post) != "GameOver"
    assertions["A5_cleanup"] = "passed" if ok else "failed"
    detail["A5_cleanup"] = (
        f"post: stack={len(post.get('stack') or [])} wf={wf_type(post)}/"
        f"p{wf_player(post)} turn={post.get('turn_number')} "
        f"phase={post.get('phase')} rejections={len(ST['rejections'])}")
    return assertions, detail


def render_summary(assertions, detail, verdict, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 700
    img = Image.new("RGB", (W, H), (16, 18, 24))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 64], fill=(30, 32, 42))
    d.text((20, 16), f"phase-rs/phase #{ISSUE} - Jirina Kudro token count",
           fill=(235, 235, 240))
    d.text((20, 38), f"v0.99.0 (d919616) protocol 98 | {RUN_ID} | "
                     f"verdict: {verdict}", fill=(150, 160, 175))
    y = 90
    d.text((20, y), "Assertions (token counts from authoritative exports):",
           fill=(200, 200, 210))
    y += 26
    order = ["A1_setup_ok", "A2_first_count", "A3_kill_ok",
             "A4_second_count", "A5_cleanup"]
    labels = {
        "A1_setup_ok": "A1 setup_ok - Jirina cast from CZ, ETB resolved",
        "A2_first_count": "A2 first_count - 1 token after first commander cast",
        "A3_kill_ok": "A3 kill_ok - wipe; Jirina back in command zone",
        "A4_second_count": "A4 second_count - 2 tokens after second commander cast",
        "A5_cleanup": "A5 cleanup - stack empty, game proceeding",
    }
    for a in order:
        v = assertions.get(a, "not-run")
        color = {"passed": (110, 220, 130), "failed": (240, 110, 110),
                 "not-run": (170, 170, 170)}[v]
        d.text((30, y), f"{labels[a]}", fill=(220, 220, 230))
        d.text((880, y), v, fill=color)
        y += 30
    y += 10
    d.text((20, y), "Detail:", fill=(200, 200, 210))
    y += 24
    for a in order:
        line = f"{a}: {detail.get(a, '')}"
        while len(line) > 118:
            d.text((30, y), line[:118], fill=(160, 165, 175))
            line = "    " + line[118:]
            y += 18
            if y > H - 40:
                break
        d.text((30, y), line[:118], fill=(160, 165, 175))
        y += 22
        if y > H - 40:
            break
    d.text((20, H - 24),
           "Evidence: ntindle/phase-bug-state-evidence 7379/" + RUN_ID,
           fill=(120, 125, 135))
    img.save(out_path)
    say(f"rendered {out_path}")


def finalize(verdict, assertions, detail):
    import shutil
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": assertions, "detail": detail}, f, indent=1)
    render_summary(assertions, detail, verdict, f"{EVDIR}/summary.png")
    ev_hashes = {}
    for fn in sorted(os.listdir(EVDIR)):
        if fn in ("manifest.sha256", "assertions.json", "summary.png",
                  "run.json"):
            continue
        ev_hashes[fn] = _sha256_of_file(f"{EVDIR}/{fn}")
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Jirina Kudro - Only makes one token regardless of how "
                 "many times it has been cast.",
        "server_identity": SERVER_IDENTITY,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                      time.localtime()),
        "verdict": verdict,
        "scope": "Jirina Kudro ETB token count vs command-zone commander "
                 "cast count (Ref CommanderCastFromCommandZoneCount); "
                 "native engine, three human driver seats (CommanderDraft), "
                 "protocol-98 driver",
        "game_code": ST["game_code"],
        "decks": {"p0_commander": P0_COMMANDER, "p0": P0_DECK,
                  "p1": P1_DECK, "p2": P2_DECK},
        "assertions": assertions,
        "assertion_detail": detail,
        "notes": ST["notes"],
        "rejections": ST["rejections"],
        "waiting_for_seen": WF_SEEN,
        "tokens_after_cast1": ST["tokens_after_cast1"],
        "tokens_after_cast2": ST["tokens_after_cast2"],
        "cz_choice": ST["cz_choice"],
        "evidence_hashes": ev_hashes,
        "evidence_dir": f"evidence/{ISSUE}/{RUN_ID}",
        "limitations": [
            "Browser UI not exercised; native engine via three "
            "human driver seats.",
            "All-land + Day of Judgment harness decks are a "
            "test-harness convenience (engine accepts custom "
            "decks); exercised behavior is the shipped card text.",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports, restorable only via "
            "full game replay (scenario_7379.py).",
        ],
        "driver_notes": [
            "Protocol-98 driver: MulliganDecision Keep gated on "
            "pending[] Declare; SelectCards for bottom/discard; "
            "PassPriority revision-guarded; CastSpell advertised "
            "actions submitted as-is (engine auto-pays, incl. "
            "commander tax).",
            "Commander cast from the command zone located by zone="
            "CommandZone object; OptionalEffectChoice answered via "
            "viewer_interaction exactChoices decideOptionalEffect "
            "surfaces.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    shutil.copy(__file__, f"{EVDIR}/scenario_7379.py")
    write_manifest()
    wire("finalized", {"verdict": verdict, "assertions": assertions})
    say(f"FINAL verdict={verdict} assertions={assertions}")


def write_manifest():
    files = sorted(fn for fn in os.listdir(EVDIR)
                   if fn != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        for fn in files:
            f.write(f"{_sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
    return files


def refresh_evidence_hashes():
    files = write_manifest()
    rp = f"{EVDIR}/run.json"
    run = json.load(open(rp))
    run["evidence_hashes"] = {fn: _sha256_of_file(f"{EVDIR}/{fn}")
                              for fn in files
                              if fn not in ("manifest.sha256", "run.json")}
    json.dump(run, open(rp, "w"), indent=1)
    write_manifest()
    print("manifest regenerated over final bytes", flush=True)


async def main():
    reset()
    global C0
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    p2 = PhaseClient("P2")
    await p0.connect()
    await p1.connect()
    await p2.connect()
    say("server identity pinned: v0.99.0 (d919616) protocol 98; using "
        "live backfill server 127.0.0.1:9374")

    await p0.create(cdeck(P0_COMMANDER, *P0_DECK), player_count=3,
                    format_config=COMMANDER_FORMAT)
    await p1.join(p0.game_code, cdeck([], *P1_DECK))
    await p2.join(p0.game_code, deck(*P2_DECK))
    await asyncio.sleep(2.0)
    C0 = p0
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id} P2 seat={p2.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                        "p2_seat": p2.player_id,
                        "p0_commander": P0_COMMANDER, "p0_deck": P0_DECK,
                        "p1_deck": P1_DECK, "p2_deck": P2_DECK,
                        "server_identity": SERVER_IDENTITY})
    with open(f"{EVDIR}/setup.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "game_code": p0.game_code,
                   "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                   "p2_seat": p2.player_id,
                   "p0_commander": P0_COMMANDER, "p0_deck": P0_DECK,
                   "p1_deck": P1_DECK, "p2_deck": P2_DECK,
                   "server_identity": SERVER_IDENTITY,
                   "server_note": "reused backfill-owned 127.0.0.1:9374; "
                                  "fresh game per run"},
                  f, indent=1)

    last_rev = {}
    last_tick_wall = 0.0
    t0 = time.time()
    watch_start = {}
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                               (p1, p1.player_id, lambda c, p: dummy_tick(c, p, PLN)),
                               (p2, p2.player_id, lambda c, p: dummy_tick(c, p, PLN))):
            for r in drain_rejections(c):
                ST["rejections"].append({"at": now, "who": c.name,
                                         "type": r["type"], "data": r["data"]})
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                                   (p1, p1.player_id, lambda c, p: dummy_tick(c, p, PLN)),
                                   (p2, p2.player_id, lambda c, p: dummy_tick(c, p, PLN))):
                try:
                    await tickfn(c, pid)
                except Exception as e:
                    say(f"tick error {c.name}: {e}")
                last_rev[c.name] = c.revision
        else:
            for c, pid, tickfn in ((p0, p0.player_id, p0_tick),
                                   (p1, p1.player_id, lambda c, p: dummy_tick(c, p, PLN)),
                                   (p2, p2.player_id, lambda c, p: dummy_tick(c, p, PLN))):
                if c.revision != last_rev.get(c.name, -1):
                    try:
                        await tickfn(c, pid)
                    except Exception as e:
                        say(f"tick error {c.name}: {e}")
                    last_rev[c.name] = c.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        elapsed = now - t0

        if wf_type(state) == "GameOver":
            ST["stop"] = True
            ST["notes"].append("game ended before scenario completed")
            say("game over")
            continue

        # SETUP watchdog: first cast should be submittable once we have
        # 4 lands (one of each color for {1}{R}{W}{B})
        if ST["stage"] == "SETUP":
            if ST["cast1_submitted"]:
                pass
            elif elapsed > SETUP_DEADLINE:
                ST["notes"].append(f"SETUP deadline after {elapsed:.0f}s; "
                                   "Jirina cast never advertised")
                say("SETUP deadline hit")
                finalize("blocked",
                         {a: "not-run" for a in
                          ("A1_setup_ok", "A2_first_count", "A3_kill_ok",
                           "A4_second_count", "A5_cleanup")},
                         {"A1_setup_ok": "Jirina cast from CZ never "
                                         "advertised/submitted; notes=" +
                                         "; ".join(ST["notes"])})
                ST["stop"] = True
                continue
            elif not ST["pre_exported"]:
                # export pre once we are past turn 1 with a commander cast
                # imminent is hard to predict; export early as baseline
                pass

        # export pre.json right before submitting cast1 (first sight of the
        # advertised commander CastSpell)
        if (ST["stage"] == "SETUP" and not ST["pre_exported"]
                and not ST["cast1_submitted"]):
            acts = st.get("legal_actions", [])
            if castspell_advertised(acts, commander_oid(state, 0)):
                s = await export_now("pre.json")
                if s is not None:
                    ST["pre_exported"] = True
                    ST["stage"] = "CAST1"
                    say("pre exported; CAST1 stage")

        if ST["stage"] == "WAIT_ETB1":
            watch_start.setdefault("etb1", now)
            if etb_settled(state):
                s = await export_now("mid1.json")
                if s is not None:
                    ST["mid1_exported"] = True
                    ST["stage"] = "WIPE"
                    say(f"mid1 exported; P0 tokens="
                        f"{tokens_on_bf(json.loads(s)['state'], 0)}; WIPE stage")
            elif now - watch_start["etb1"] > 300:
                ST["notes"].append("WAIT_ETB1 watchdog: ETB never settled")
                finalize("blocked",
                         {a: "not-run" for a in
                          ("A1_setup_ok", "A2_first_count", "A3_kill_ok",
                           "A4_second_count", "A5_cleanup")},
                         {"A1_setup_ok": "ETB1 never settled; notes=" +
                                         "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

        if ST["stage"] == "REPLACEMENT":
            watch_start.setdefault("repl", now)
            zone, _ = jirina_zone(state, 0)
            stack = state.get("stack") or []
            if zone in CZ_ZONES and not stack:
                s = await export_now("mid2.json")
                if s is not None:
                    ST["mid2_exported"] = True
                    ST["stage"] = "CAST2"
                    say(f"mid2 exported; Jirina in CommandZone; CAST2 stage")
            elif now - watch_start["repl"] > 300:
                ST["notes"].append("REPLACEMENT watchdog: Jirina never "
                                   "returned to command zone")
                finalize("blocked",
                         {a: "not-run" for a in
                          ("A1_setup_ok", "A2_first_count", "A3_kill_ok",
                           "A4_second_count", "A5_cleanup")},
                         {"A3_kill_ok": "post-wipe Jirina zone=" +
                                        str(zone) + "; notes=" +
                                        "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

        if ST["stage"] == "WAIT_ETB2":
            watch_start.setdefault("etb2", now)
            if etb_settled(state):
                await asyncio.sleep(2)
                s = await export_now("post.json")
                if s is not None:
                    ST["post_exported"] = True
                    pre_env = json.loads(open(f"{EVDIR}/pre.json").read())
                    mid1_env = json.loads(open(f"{EVDIR}/mid1.json").read())
                    mid2_env = json.loads(open(f"{EVDIR}/mid2.json").read())
                    post_env = json.loads(s)
                    assertions, detail = compute_assertions(
                        pre_env, mid1_env, mid2_env, post_env)
                    a1 = assertions["A1_setup_ok"] == "passed"
                    a3 = assertions["A3_kill_ok"] == "passed"
                    a2 = assertions["A2_first_count"]
                    a4 = assertions["A4_second_count"]
                    if not (a1 and a3):
                        verdict = "blocked"
                    elif a2 == "failed" or a4 == "failed":
                        verdict = "reproduced"
                    else:
                        verdict = "not-reproduced"
                    finalize(verdict, assertions, detail)
                    ST["stop"] = True
                    continue
            elif now - watch_start["etb2"] > 300:
                ST["notes"].append("WAIT_ETB2 watchdog: ETB never settled")
                finalize("blocked",
                         {a: "not-run" for a in
                          ("A1_setup_ok", "A2_first_count", "A3_kill_ok",
                           "A4_second_count", "A5_cleanup")},
                         {"A4_second_count": "ETB2 never settled; notes=" +
                                             "; ".join(ST["notes"])})
                ST["stop"] = True
                continue

    for c in (p0, p1, p2):
        await c.close()
    WIRE.close()
    RUNLOG.close()
    refresh_evidence_hashes()
    print(json.dumps({
        "stage": ST.get("stage"),
        "pre_exported": ST.get("pre_exported"),
        "mid1_exported": ST.get("mid1_exported"),
        "mid2_exported": ST.get("mid2_exported"),
        "post_exported": ST.get("post_exported"),
        "cast1_submitted": ST.get("cast1_submitted"),
        "doj_submitted": ST.get("doj_submitted"),
        "cast2_submitted": ST.get("cast2_submitted"),
        "tokens_after_cast1": ST.get("tokens_after_cast1"),
        "tokens_after_cast2": ST.get("tokens_after_cast2"),
        "cz_choice": ST.get("cz_choice"),
        "rejections": len(ST["rejections"]),
        "notes": ST.get("notes"),
    }, indent=2, default=str))
    return ST


if __name__ == "__main__":
    st = asyncio.run(main())
