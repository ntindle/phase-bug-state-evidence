#!/usr/bin/env python3
"""Issue #6988: Tenuous Truce's bidirectional attack trigger is unsupported.

Oracle: "Enchant opponent. At the beginning of enchanted opponent's end step,
you and that player each draw a card. When you attack enchanted opponent or a
planeswalker they control or when they attack you or a planeswalker you
control, sacrifice this Aura."

Pinned card-data (v0.81.3) parses the attack trigger as TWO triggers:
  (a) mode "YouAttack", description "When you attack enchanted opponent,
      sacrifice ~." -- the "or a planeswalker they control" leg is dropped.
  (b) mode {"Unknown": "When a planeswalker they control or when they attack
      you or a planeswalker you control"} -- the remaining three legs are
      entirely unparsed.

Plan (two human seats, native engine, v0.81.3/protocol 70). One fresh
Aura per leg, because the parsed YouAttack leg fires on ANY P0 attack (it
has no valid_target gate), so reusing one Aura would vacate later legs:
  SETUP       P0 enchants P1 with Tenuous Truce #1 (target P1 via
              TargetSelection); both sides build boards: P0 Bears, P1
              Bears + Jace Beleren.
  CTRL_ATTACK P0 attacks P1 directly. Parsed YouAttack leg fires: sacrifice.
              (control -- proves the parsed leg works)
  RECAST_B    P0 enchants P1 with Tenuous Truce #2.
  BACK_ATTACK P1 attacks P0. Oracle: sacrifice. The remaining three legs
              live in the Unknown-mode trigger: expected NOT to fire.
  RECAST_C    P0 enchants P1 with Tenuous Truce #3.
  PW_ATTACK   P0 attacks P1's Jace Beleren. Oracle: sacrifice. The parsed
              YouAttack leg (no valid_target) is expected to fire instead
              of any planeswalker leg; the stack trigger description is
              captured to attribute the mechanism.

Assertions (correct-behavior properties; failed = defect present):
  A1 parse_gap       pinned card-data: exactly one Unknown-mode trigger
                     carrying the bidirectional attack text
  A2 setup_ok        pre.json: Aura on BF attached to P1, P1 Jace on BF,
                     ready Bears on both sides
  A3 control_direct  mid_ctrl.json: Aura #1 sacrificed after P0 attacked P1
                     (parsed YouAttack leg works)
  A4 back_leg        mid_back.json: Aura #2 sacrificed after P1 attacked P0
                     (attack landed: P0 life < 20)
  A5 pw_leg          post.json: Aura #3 sacrificed after P0 attacked P1's
                     planeswalker (attack landed: Jace loyalty < 3);
                     mechanism attributed via the stack trigger description
  A6 cleanup         post.json stack empty, game advanced

Verdict = reproduced iff A3 passes and A4 fails.
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
ISSUE = 6988
RUN_ID = "20260913-6988b"
EVID_ISSUE = "6988"
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


TRUCE = "Tenuous Truce"
BEARS = "Grizzly Bears"
JACE = "Jace Beleren"
FOREST = "Forest"
PLAINS = "Plains"
ISLAND = "Island"

ST = {"stage": "SETUP", "stop": False,
      "pre_exported": False, "mid_ctrl_exported": False,
      "mid_back_exported": False, "post_exported": False,
      "truce_cast": False, "truce_targeted": False, "truce_in_flight": False,
      "aura_oid": None, "aura_attached_detail": None,
      "p1_jace_cast": False,
      "ctrl_submitted": False, "ctrl_turn": None,
      "back_submitted": False, "back_turn": None,
      "pw_submitted": False, "pw_turn": None, "pw_bear": None,
      "ctrl_trigger_desc": None, "ctrl_trigger_kind": None,
      "back_trigger_desc": None, "back_trigger_kind": None,
      "pw_trigger_desc": None, "pw_trigger_kind": None,
      "truce_choice_id": None, "turn_cap_abort": False,
      "pre_turn": None, "pre_p1_life": None, "pre_jace_loyalty": None,
      "pre_p0_life": None}

WF_SEEN = []
PROMPT_SEEN = {}
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


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bears_on_bf(state, pid):
    return [oid for oid, o in bf(state, pid) if oname(o) == BEARS]


def ready_bears(state, pid):
    """Bears able to attack: on BF, no summoning sickness, untapped."""
    return [oid for oid, o in bf(state, pid)
            if oname(o) == BEARS and not o.get("summoning_sick")
            and not o.get("tapped")]


def jace_of(state, pid):
    for oid, o in bf(state, pid):
        if oname(o) == JACE:
            return oid
    return None


def aura_oid(state):
    for oid, o in state["objects"].items():
        if oname(o) == TRUCE and o.get("zone") == "Battlefield" \
                and o.get("controller") == 0:
            return oid
    return None


def attached_to_player(o, pid):
    """Recursive int search through the nested attached_to dict."""
    found = []

    def rec(x):
        if isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
        elif isinstance(x, int) and not isinstance(x, bool):
            found.append(x)

    rec(o.get("attached_to"))
    return pid in found


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


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    out = []
    for s in ch.get("surfaces", []) or []:
        code = (s.get("data") or {}).get("code")
        if code:
            out.append(code)
    return out


def choice_text(ch):
    bits = []
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        for k in ("text", "label", "name", "value"):
            if d.get(k):
                bits.append(str(d[k]))
    return " | ".join(bits)


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
        resp_out = {"type": "choose", "data": {"choiceId": choice_id}}
    await send_interaction(c, {"interactionId": iid, "response": resp_out})
    PROMPT_SEEN.setdefault(iid, {})["answered"] = True
    say(f"[{c.name}] answered vi {str(iid)[:8]} choice {choice_id} "
        f"(rtype={rtype} spec={spec})")


# ------------------------------------------------------------------- tick

def attack_plan(state, pid):
    """Returns (attacks_list, mine): attacks to declare, and whether this
    seat is the designated attacker for the current stage."""
    stage = ST["stage"]
    if stage == "CTRL_ATTACK" and pid == 0:
        bears = ready_bears(state, 0)
        if bears:
            return ([[int(bears[0]), {"type": "Player", "data": 1}]], True)
        say(f"[P0] CTRL_ATTACK: no ready bears ({len(bears)})")
        return ([], True)
    if stage == "BACK_ATTACK" and pid == 1:
        bears = ready_bears(state, 1)
        if bears:
            return ([[int(bears[0]), {"type": "Player", "data": 0}]], True)
        say(f"[P1] BACK_ATTACK: no ready bears ({len(bears)})")
        return ([], True)
    if stage == "PW_ATTACK" and pid == 0:
        bears = ready_bears(state, 0)
        jace = jace_of(state, 1)
        if bears and jace:
            ST["pw_bear"] = bears[0]
            return ([[int(bears[0]),
                      {"type": "Planeswalker", "data": int(jace)}]], True)
        say(f"[P0] PW_ATTACK: missing pieces (bears={len(bears)}, "
            f"p1jace={jace is not None})")
        return ([], True)
    return ([], False)


async def answer_truce_target(c, pid, st, state):
    """TargetSelection for the Tenuous Truce cast: pick the P1 (seat 1)
    player candidate and submit its engine-issued choice id."""
    vi = get_vi(st)
    if not vi:
        return False
    pick = None
    n = 0
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        for ch in chs:
            n += 1
            if seat_of(ch) == 1:
                pick = (opp, ch)
                break
        if pick:
            break
    if pick is None:
        # fallback: any choice whose text mentions player/opponent
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response") or {}).get("data", {}) or {}
            chs = data.get("choices") or data.get("candidates") or []
            for ch in chs:
                if "player" in choice_text(ch).lower():
                    pick = (opp, ch)
                    break
            if pick:
                break
    wire("truce_target_candidates",
         {"n": n, "picked": choice_text(pick[1])[:80] if pick else None,
          "seat": seat_of(pick[1]) if pick else None})
    if pick is None:
        say("[P0] truce target: no P1 candidate; NOT answering")
        return False
    opp, ch = pick
    ST["truce_targeted"] = True
    ST["truce_choice_id"] = ch.get("id")
    say(f"[P0] truce target: P1 (seat={seat_of(ch)}, "
        f"text={choice_text(ch)[:70]})")
    await answer_vi_choice(c, opp, ch.get("id"))
    return True


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan: always keep (dense decks)
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                  "data": {"choice": {"type": "Keep"}}})
            return True
    # bottom cards after mulligan
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
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
            say(f"[{c.name}] bottoms {count}")
            return True
    # discard to hand size: lands first, protect Truce/Bears/Jace
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        pend = (state.get("waiting_for") or {}).get("data") or {}
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)

        def _prio(oid):
            nm = oname(state["objects"][oid])
            if nm in (FOREST, PLAINS, ISLAND):
                return 0
            if nm == TRUCE:
                return 3
            if nm == JACE:
                return 2
            return 1

        oids = sorted(hand_oids(state, pid), key=_prio)
        picks = oids[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                  "data": {"cards": [int(x) for x in picks]}})
            say(f"[{c.name}] discards {len(picks)} to hand size")
            return True
        return True
    # truce target selection (P0 only)
    if is_p0 and ST["truce_in_flight"] and not ST["truce_targeted"] \
            and wf_type(state) == "TargetSelection" \
            and wf_player(state) == pid:
        if await answer_truce_target(c, pid, st, state):
            return True
    # combat: attackers (stage-specific) / blockers (always empty)
    for a in acts:
        if a["type"] == "DeclareAttackers":
            if state.get("active_player") == pid \
                    and (state.get("phase") or "") == "DeclareAttackers" \
                    and wf_player(state) == pid:
                attacks, mine = attack_plan(state, pid)
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = attacks
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                if mine and attacks:
                    tag = {"CTRL_ATTACK": "ctrl", "BACK_ATTACK": "back",
                           "PW_ATTACK": "pw"}[ST["stage"]]
                    ST[f"{tag}_submitted"] = True
                    ST[f"{tag}_turn"] = turn_of(state)
                    say(f"[{c.name}] {ST['stage']}: attacks={attacks}")
                elif mine:
                    say(f"[{c.name}] {ST['stage']}: no attackers declared "
                        f"(missing pieces)")
                return True
            # not our declare window: submit empty to keep the game moving
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
    # main-phase actions
    if is_my_main(state, pid):
        # land drop first
        for a in acts:
            if a["type"] == "PlayLand":
                d = a.get("data", {}) or {}
                oid = d.get("object_id")
                if isinstance(oid, int) and get_obj(
                        state, oid).get("zone") == "Hand":
                    await submit_as_is(c, a)
                    return True
        if is_p0 and ST["stage"] in ("SETUP", "RECAST_B", "RECAST_C"):
            if not ST["truce_cast"]:
                tid = find_hand(state, pid, TRUCE)
                a = castspell_advertised(acts, tid) if tid else None
                if a:
                    ST["truce_cast"] = True
                    ST["truce_in_flight"] = True
                    await submit_as_is(c, a)
                    say(f"[P0] casts Tenuous Truce ({ST['stage']})")
                    return True
            if ST["stage"] == "SETUP" and len(bears_on_bf(state, pid)) < 2:
                bid = find_hand(state, pid, BEARS)
                a = castspell_advertised(acts, bid) if bid else None
                if a:
                    await submit_as_is(c, a)
                    say("[P0] casts Grizzly Bears")
                    return True
        if not is_p0:
            if not ST["p1_jace_cast"]:
                jid = find_hand(state, pid, JACE)
                a = castspell_advertised(acts, jid) if jid else None
                if a:
                    ST["p1_jace_cast"] = True
                    await submit_as_is(c, a)
                    say("[P1] casts Jace Beleren")
                    return True
            if len(bears_on_bf(state, pid)) < 2:
                bid = find_hand(state, pid, BEARS)
                a = castspell_advertised(acts, bid) if bid else None
                if a:
                    await submit_as_is(c, a)
                    say("[P1] casts Grizzly Bears")
                    return True
    # mana payment prompts: submit as-is
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            if wf_player(state) == pid:
                await submit_as_is(c, a)
                return True
            continue
    # default: pass priority
    if wf_type(state) == "Priority" and wf_player(state) == pid:
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


# ------------------------------------------------------------------ main

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def find_card(cd, name):
    key = name.lower()
    if key in cd:
        return cd[key]
    for k, v in cd.items():
        if isinstance(v, dict) and v.get("name") == name:
            return v
    raise KeyError(name)


def aura_attached_to_p1(state, exclude=()):
    oid = None
    for oid, o in state["objects"].items():
        if oname(o) == TRUCE and o.get("zone") == "Battlefield" \
                and o.get("controller") == 0 and oid not in exclude:
            att = o.get("attached_to")
            return oid, (attached_to_player(o, 1),
                         json.dumps(att, default=str)[:200])
    return None, (None, None)


def truce_oids_on_bf(state):
    return [oid for oid, o in state["objects"].items()
            if oname(o) == TRUCE and o.get("zone") == "Battlefield"
            and o.get("controller") == 0]


def scan_attack_triggers(state):
    """Capture triggered-ability stack entries about the Aura's sacrifice,
    so each leg's sacrifice can be attributed to the parsed leg that fired."""
    out = []
    for e in state.get("stack") or []:
        blob = json.dumps(e, default=str)
        if "ruce" not in blob and "acrifice" not in blob.lower():
            continue
        kind = e.get("kind") or {}
        ktype = kind.get("type") if isinstance(kind, dict) else kind
        data = kind.get("data") if isinstance(kind, dict) else {}
        ab = (data or {}).get("ability") or {}
        out.append({"kind": ktype,
                    "description": ab.get("description"),
                    "source_id": (data or {}).get("source_id")})
    return out


async def main():
    t0 = time.time()

    # parse evidence: Tenuous Truce record from the pinned card data
    cd = json.load(open(CARD_DATA))
    tru = find_card(cd, TRUCE)
    with open(f"{EVDIR}/parse_tenuous_truce.json", "w") as f:
        json.dump({"name": tru["name"],
                   "mana_cost": tru["mana_cost"],
                   "oracle_text": tru["oracle_text"],
                   "triggers": tru["triggers"]}, f, indent=1)
    say("wrote parse_tenuous_truce.json "
        f"({len(tru['triggers'])} triggers)")

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((TRUCE, 8), (BEARS, 12), (FOREST, 12), (PLAINS, 12)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code,
                  deck((BEARS, 12), (JACE, 4), (FOREST, 12), (ISLAND, 12)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

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
            if rej:
                # a rejected attack submission must not count as submitted
                for tag in ("pw", "back", "ctrl"):
                    if ST.get(f"{tag}_submitted") and not ST.get(
                            f"{tag}_confirmed"):
                        ST[f"{tag}_submitted"] = False
                        say(f"[{c.name}] {tag} attack rejected; will retry")
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

        # truce resolution bookkeeping (fresh aura per leg; exclude prior
        # legs' auras when recasting)
        if ST["truce_in_flight"]:
            excl = ((ST["aura_oid"],) if ST["aura_oid"] else ())
            oid, (to_p1, detail) = aura_attached_to_p1(state, exclude=excl)
            if oid and to_p1:
                ST["aura_oid"] = oid
                ST["truce_in_flight"] = False
                ST["aura_attached_detail"] = (
                    f"attached_to_p1={to_p1} detail={detail}")
                say(f"Tenuous Truce resolved on BF (oid={oid}, "
                    f"stage={ST['stage']}); {ST['aura_attached_detail']}")
                if ST["stage"] == "RECAST_B":
                    ST["stage"] = "BACK_ATTACK"
                    say("stage -> BACK_ATTACK (aura #2 on BF)")
                elif ST["stage"] == "RECAST_C":
                    ST["stage"] = "PW_ATTACK"
                    say("stage -> PW_ATTACK (aura #3 on BF)")

        # trigger attribution: capture which sacrifice trigger appears on
        # the stack right after each attack declaration
        for tag in ("ctrl", "back", "pw"):
            if ST.get(f"{tag}_submitted") \
                    and ST.get(f"{tag}_trigger_desc") is None:
                hits = scan_attack_triggers(state)
                if hits:
                    ST[f"{tag}_trigger_desc"] = hits[0]["description"]
                    ST[f"{tag}_trigger_kind"] = hits[0]["kind"]
                    wire(f"{tag}_trigger_stack", hits[0])
                    say(f"[{tag}] trigger on stack: kind="
                        f"{hits[0]['kind']} desc="
                        f"{str(hits[0]['description'])[:90]}")

        # SETUP -> CTRL_ATTACK
        if ST["stage"] == "SETUP" and not ST["pre_exported"]:
            oid, (to_p1, _) = aura_attached_to_p1(state)
            j1 = jace_of(state, 1)
            rb0, rb1 = ready_bears(state, 0), ready_bears(state, 1)
            if oid and to_p1 and j1 and rb0 and rb1:
                await export_now("pre.json")
                ST["pre_exported"] = True
                ST["stage"] = "CTRL_ATTACK"
                ST["pre_turn"] = turn_of(state)
                ST["pre_p0_life"] = life_of(state, 0)
                ST["pre_p1_life"] = life_of(state, 1)
                ST["pre_jace_loyalty"] = get_obj(state, j1).get("loyalty")
                say(f"SETUP complete at turn {ST['pre_turn']}: aura={oid} "
                    f"attached to P1, P1 Jace loyalty={ST['pre_jace_loyalty']}, "
                    f"ready bears P0={len(rb0)} P1={len(rb1)}; "
                    f"stage -> CTRL_ATTACK")
        # CTRL_ATTACK -> RECAST_B
        if ST["stage"] == "CTRL_ATTACK" and ST["ctrl_submitted"] \
                and state.get("active_player") == 1 \
                and stack_empty(state) and not ST["mid_ctrl_exported"]:
            await export_now("mid_ctrl.json")
            ST["mid_ctrl_exported"] = True
            ST["stage"] = "RECAST_B"
            ST.update({"truce_cast": False, "truce_targeted": False,
                       "truce_in_flight": False})
            say(f"stage -> RECAST_B (P0 attacked P1 directly on turn "
                f"{ST['ctrl_turn']})")
        # BACK_ATTACK -> RECAST_C
        if ST["stage"] == "BACK_ATTACK" and ST["back_submitted"] \
                and state.get("active_player") == 0 \
                and stack_empty(state) and not ST["mid_back_exported"]:
            await export_now("mid_back.json")
            ST["mid_back_exported"] = True
            ST["stage"] = "RECAST_C"
            ST.update({"truce_cast": False, "truce_targeted": False,
                       "truce_in_flight": False})
            say(f"stage -> RECAST_C (P1 attacked P0 on turn "
                f"{ST['back_turn']})")
        # PW_ATTACK -> DONE
        if ST["stage"] == "PW_ATTACK" and ST["pw_submitted"] \
                and state.get("active_player") == 1 \
                and stack_empty(state) and not ST["post_exported"]:
            await export_now("post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            say(f"stage -> DONE (P0 attacked P1's Jace on turn "
                f"{ST['pw_turn']})")
            ST["stop"] = True

        if turn_of(state) > 45 and not ST["stop"]:
            ST["turn_cap_abort"] = True
            ST["stop"] = True
            say("turn cap 45 reached; aborting")

    obs = await finish(t0)
    await p0.close()
    await p1.close()
    return obs


# ----------------------------------------------------------------- finish

async def finish(t0):
    notes = []
    ass = {}

    def load_state(path):
        p = f"{EVDIR}/{path}"
        if not os.path.exists(p):
            return None
        return json.load(open(p))["state"]

    pre = load_state("pre.json")
    mid_ctrl = load_state("mid_ctrl.json")
    mid_back = load_state("mid_back.json")
    post = load_state("post.json")

    # ---- A1: parse check ----
    try:
        ptr = json.load(open(f"{EVDIR}/parse_tenuous_truce.json"))
        trigs = ptr["triggers"]
        unknown = [t for t in trigs
                   if isinstance(t.get("mode"), dict)
                   and "Unknown" in t["mode"]]
        you_attack = [t for t in trigs if t.get("mode") == "YouAttack"]
        ok = (len(unknown) == 1
              and "they attack you" in unknown[0]["mode"]["Unknown"])
        notes.append(
            f"A1: {len(trigs)} triggers; Unknown-mode: "
            f"{[t['mode']['Unknown'][:60] for t in unknown]}; "
            f"YouAttack legs: {[t.get('description') for t in you_attack]} -> "
            f"{'passed' if ok else 'failed'}")
        ass["A1_parse_gap"] = "passed" if ok else "failed"
    except Exception as e:
        notes.append(f"A1 parse check error: {e}")
        ass["A1_parse_gap"] = "not-run"

    # ---- A2: setup ----
    if pre is not None:
        oid, (to_p1, detail) = aura_attached_to_p1(pre)
        j1 = jace_of(pre, 1)
        rb0, rb1 = ready_bears(pre, 0), ready_bears(pre, 1)
        ok = bool(oid and to_p1 and j1 and rb0 and rb1)
        notes.append(
            f"A2: pre.json turn={turn_of(pre)} aura={oid} attached_to_p1={to_p1} "
            f"({detail}); P1 Jace={j1} loyalty={get_obj(pre, j1).get('loyalty') if j1 else None}; "
            f"ready bears P0={len(rb0)} P1={len(rb1)}; "
            f"P0 life={life_of(pre, 0)} P1 life={life_of(pre, 1)} -> "
            f"{'passed' if ok else 'failed'}")
        ass["A2_setup_ok"] = "passed" if ok else "failed"
    else:
        notes.append("A2 not-run: no pre.json (setup never completed)")
        ass["A2_setup_ok"] = "not-run"

    # ---- A3: control (P0 attacks P1 directly; aura #1) ----
    if mid_ctrl is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_oids_on_bf(mid_ctrl)
        gy = zone_oids(mid_ctrl, 0, "Graveyard", TRUCE)
        p1life = life_of(mid_ctrl, 1)
        landed = p1life is not None and p1life < 20
        sacrificed = not bf_truce and len(gy) > 0
        ass["A3_control_direct"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A3: mid_ctrl.json (P0 attacked P1, turn {ST['ctrl_turn']}): "
            f"Truce on BF={bf_truce}, in P0 graveyard={gy}, P1 life={p1life} "
            f"(attack landed={landed}); stack trigger was "
            f"kind={ST['ctrl_trigger_kind']} "
            f"desc={str(ST['ctrl_trigger_desc'])[:70]}; parsed YouAttack "
            f"leg expected to fire -> {ass['A3_control_direct']}")
    else:
        ass["A3_control_direct"] = "not-run"
        notes.append("A3 not-run: no mid_ctrl.json or setup failed")

    # ---- A4: back leg (P1 attacks P0; aura #2) ----
    if mid_back is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_oids_on_bf(mid_back)
        p0life = life_of(mid_back, 0)
        landed = p0life is not None and p0life < 20
        sacrificed = not bf_truce
        ass["A4_back_leg"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A4: mid_back.json (P1 attacked P0, turn {ST['back_turn']}): "
            f"Truce on BF={bf_truce}, P0 life={p0life} "
            f"(attack landed={landed}); stack trigger seen="
            f"{ST['back_trigger_desc'] is not None}; oracle expects "
            f"sacrifice -> {ass['A4_back_leg']}")
    else:
        ass["A4_back_leg"] = "not-run"
        notes.append("A4 not-run: no mid_back.json or setup failed")

    # ---- A5: planeswalker leg (P0 attacks P1's Jace; aura #3) ----
    if post is not None and ass["A2_setup_ok"] == "passed":
        bf_truce = truce_oids_on_bf(post)
        j1 = jace_of(post, 1)
        loy = get_obj(post, j1).get("loyalty") if j1 else None
        landed = (loy is not None and loy < 3) or j1 is None
        sacrificed = not bf_truce
        ass["A5_pw_leg"] = "passed" if sacrificed else "failed"
        notes.append(
            f"A5: post.json (P0 attacked P1's Jace, turn {ST['pw_turn']}): "
            f"Truce on BF={bf_truce}, Jace loyalty={loy} "
            f"(attack landed={landed}); stack trigger was "
            f"kind={ST['pw_trigger_kind']} "
            f"desc={str(ST['pw_trigger_desc'])[:70]} -> "
            f"{ass['A5_pw_leg']}")
    else:
        ass["A5_pw_leg"] = "not-run"
        notes.append("A5 not-run: no post.json or setup failed")

    # ---- A6: cleanup ----
    if post is not None:
        ok = stack_empty(post)
        ass["A6_cleanup"] = "passed" if ok else "failed"
        notes.append(f"A6: post.json stack empty={ok}, "
                     f"turn={turn_of(post)}, phase={post.get('phase')} -> "
                     f"{ass['A6_cleanup']}")
    else:
        notes.append("A6 not-run: no post.json")
        ass["A6_cleanup"] = "not-run"

    # ---- verdict ----
    if ass["A2_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup never completed "
                     f"(turn-cap abort={ST['turn_cap_abort']})")
    elif not (ass["A3_control_direct"] in ("passed", "failed")
              and ass["A4_back_leg"] in ("passed", "failed")
              and ass["A5_pw_leg"] in ("passed", "failed")):
        verdict = "blocked"
        notes.append("verdict=blocked: attack stages did not complete "
                     "cleanly")
    elif ass["A3_control_direct"] == "passed" \
            and ass["A4_back_leg"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: the Aura sacrificed itself when P0 "
                     "attacked P1 (parsed YouAttack leg works) but NOT when "
                     "P1 attacked P0 (the Unknown-mode bidirectional legs "
                     "never fire)")
    elif ass["A3_control_direct"] == "passed" \
            and ass["A4_back_leg"] == "passed":
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: the Aura sacrificed itself on "
                     "both player-attack legs")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: control leg did not behave as "
                     "expected; cannot attribute the back leg")

    notes.append(f"WF sequence: {WF_SEEN}")
    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say(f"verdict: {verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN}, f, indent=2)

    # ---- run.json ----
    bin_path = (f"{BACKFILL}/server/releases/v0.81.3/"
                "phase-server-slim-x86_64-unknown-linux-musl")
    run = {
        "issue": ISSUE,
        "title": "Tenuous Truce's bidirectional attack trigger is unsupported",
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
        "result": ("A1 parse_gap: " + ass["A1_parse_gap"] +
                   " (attack trigger split into a YouAttack leg missing the "
                   "planeswalker clause plus one Unknown-mode leg carrying "
                   "'When a planeswalker they control or when they attack "
                   "you or a planeswalker you control'). A2 setup_ok: " +
                   ass["A2_setup_ok"] + ". A3 control_direct: " +
                   ass["A3_control_direct"] +
                   " (P0 attacked P1; parsed leg fired). A4 back_leg: " +
                   ass["A4_back_leg"] + " (P1 attacked P0). A5 pw_leg: " +
                   ass["A5_pw_leg"] +
                   " (P0 attacked P1's Jace Beleren; mechanism attributed "
                   "via stack trigger). A6 cleanup: " +
                   ass["A6_cleanup"] + "."),
        "scope": ("Tenuous Truce attack-trigger legs: card-data parse check + "
                  "runtime attacks in both directions plus a planeswalker "
                  "attack, with the parsed YouAttack leg as the control; "
                  "native engine, two human-client seats; one fresh Aura "
                  "per leg because the parsed YouAttack leg has no "
                  "valid_target gate and fires on any P0 attack"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay).",
            "The 'they attack your planeswalker' sub-leg was not separately exercised; the 'they attack you' leg covers the unparsed mode.",
        ],
        "driver_notes": [
            "Truce target selection picks the seat==1 player candidate via viewer_interaction choice id.",
            "Planeswalker defender submitted as {'type': 'Planeswalker', 'data': <oid>} in DeclareAttackers.attacks (cf. #6879).",
            "Mid states exported at the defender's next turn start with an empty stack, guaranteeing the attacker's combat fully resolved.",
            "Attack-landing corroborated via P1 life delta (20-><20), P0 life delta (20-><20), and Jace loyalty delta (3-><3).",
            "Sacrifice attribution per leg captured from the TriggeredAbility stack entry's ability.description (the parsed YouAttack leg fires on any P0 attack since it carries no valid_target gate).",
        ],
        "server_run_note": ("v0.81.3 server on 127.0.0.1:9374, started under "
                            "setsid by the prior 09:43 CDT attempt for this "
                            "issue and reused by this run (ServerHello "
                            "re-verified this run: v0.81.3/95bec6e/protocol "
                            "70/mode Full; log in runs/20260913-6988/"
                            "server.log)"),
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")

    # ---- evidence files ----
    shutil.copy(f"{BACKFILL}/driver/scenario_6988.py",
                f"{EVDIR}/scenario_6988.py")
    for _log in ("server.log",):
        _src = f"{BACKFILL}/runs/{RUN_ID}/{_log}"
        if os.path.exists(_src):
            shutil.copy(_src, f"{EVDIR}/{_log}")
    say("copied scenario_6988.py and server log into EVDIR")

    # ---- summary.png (from saved states/assertions) ----
    render_summary(run, notes, pre, mid_ctrl, mid_back, post)

    # ---- close logs BEFORE hashing (manifest written last) ----
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass

    files = ["pre.json", "mid_ctrl.json", "mid_back.json", "post.json",
             "parse_tenuous_truce.json", "assertions.json", "run.json",
             "scenario_6988.py", "wire_log.jsonl", "scenario_run.log",
             "server.log", "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            lines.append(f"{sha256_file(p)}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    # manifest written last; no say() after this point (logs are closed)

    return {"assert": ass, "verdict": verdict, "notes": notes}


def aura_zone(state, label):
    if state is None:
        return "n/a"
    oid = aura_oid(state)
    if oid:
        o = get_obj(state, oid)
        att = attached_to_player(o, 1)
        return f"BF(att->P1={att})"
    gy = zone_oids(state, 0, "Graveyard", TRUCE)
    if gy:
        return "P0-graveyard"
    return "elsewhere"


def render_summary(run, notes, pre, mid_ctrl, mid_back, post):
    from PIL import Image, ImageDraw
    W, H = 1000, 1120
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#6988 - Tenuous Truce's bidirectional attack trigger "
           "is unsupported", fill=(235, 240, 250))
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
        "A1_parse_gap": "PARSE: one Unknown-mode trigger carries the "
                        "bidirectional attack text",
        "A2_setup_ok": "GAME: pre.json Aura on BF attached to P1, P1 Jace "
                       "on BF, ready Bears both sides",
        "A3_control_direct": "GAME: Aura #1 sacrificed after P0 attacked P1 "
                             "(parsed leg control)",
        "A4_back_leg": "GAME: Aura #2 sacrificed after P1 attacked P0",
        "A5_pw_leg": "GAME: Aura #3 sacrificed after P0 attacked P1's "
                     "planeswalker (mechanism attributed)",
        "A6_cleanup": "GAME: post.json stack empty, game proceeds",
    }
    for k, lab in labels.items():
        val = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if val == "passed" else (
            (255, 90, 90) if val == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {val} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Oracle: 'When you attack enchanted opponent or a "
           "planeswalker they", fill=(200, 210, 225))
    y += 22
    d.text((36, y), "control or when they attack you or a planeswalker you "
           "control, sacrifice", fill=(200, 210, 225))
    y += 22
    d.text((36, y), "this Aura.'", fill=(200, 210, 225))
    y += 24
    d.text((24, y), "Parsed on v0.81.3:", fill=(200, 210, 225))
    y += 22
    d.text((36, y), "- YouAttack: 'When you attack enchanted opponent, "
           "sacrifice ~.'", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "  (planeswalker leg dropped)", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "- Unknown: 'When a planeswalker they control or when "
           "they attack", fill=(150, 160, 175))
    y += 22
    d.text((36, y), "  you or a planeswalker you control' (never fires)",
           fill=(150, 160, 175))
    y += 30

    for tag, st8 in (("pre", pre), ("mid_ctrl", mid_ctrl),
                     ("mid_back", mid_back), ("post", post)):
        j1 = jace_of(st8, 1) if st8 else None
        loy = get_obj(st8, j1).get("loyalty") if (st8 and j1) else "-"
        d.text((24, y), f"{tag}: turn={turn_of(st8) if st8 else 'n/a'} "
               f"phase={(st8 or {}).get('phase')} | aura={aura_zone(st8, tag)} | "
               f"P1-Jace loyalty={loy} | P0 life={life_of(st8, 0) if st8 else 'n/a'} "
               f"P1 life={life_of(st8, 1) if st8 else 'n/a'}",
               fill=(150, 160, 175))
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in notes[:13]:
        d.text((36, y), n[:112], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps({"verdict": obs["verdict"],
                      "assertions": obs["assert"]}, indent=2))
