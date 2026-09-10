#!/usr/bin/env python3
"""Issue #6609: "[Card Bug] Nethergoyf: Forces to exile all cards when escaping".

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-24, pascalberger, build 0.36.0): escaping Nethergoyf from the
graveyard "expects to exile all cards from graveyard".

Oracle text (verified from pinned v0.78.0 card-data.json):
  "Nethergoyf's power is equal to the number of card types among cards in your
  graveyard and its toughness is equal to that number plus 1.
  Escape -- {2}{B}, Exile any number of other cards from your graveyard with
  four or more card types among them. (You may cast this card from your
  graveyard for its escape cost.)"

Parsed (v0.78.0 data): Escape = NonMana Composite [Mana {2}{B}, EffectCost
  ChangeZone(Graveyard -> Exile, target Typed Card controller You properties
  [Another, InZone Graveyard])]. Both qualifiers are dropped: no player-chosen
  count ("any number of") and no aggregate "four or more card types among
  them" constraint. Confirms the triage analysis (mike-theDude, 2026-07-26).

Triage acceptance criteria:
  - Escaping Nethergoyf prompts the player to choose which other graveyard
    cards to exile, rather than exiling all of them.
  - The chosen set is rejected unless it covers four or more card types
    in total.
  - Cards not chosen remain in the graveyard, and Nethergoyf's power and
    toughness recompute from what remains.

Scenario (native engine, two human-client seats):
  P0: 8x Nethergoyf + 8x Ornithopter (artifact) + 8x Rancor (enchantment) +
      8x Shock (instant) + 8x Divination (sorcery) + 8x Grizzly Bears
      (creature) + 8x Swamp + 8x Island (dense counts: engine accepts
      >4-of for custom games; mulligan hunts 2+ lands with a Swamp).
      Only {B}/{U} spells are castable; Shock/Bears/Rancor fill the
      graveyard via cleanup discards.
  P1: 12x Lightning Bolt + 36x Mountain (removal bot; never attacks).

  SETUP: P0 casts Nethergoyf (needs Swamp+1), P1 Bolts it -> Nethergoyf in
      P0 gy. P0 casts Ornithopter (free; P1 Bolts it when able) and
      Divination ({2}{U}; draws 2, sorcery -> gy). Shock/Bears/Rancor are
      uncastable (no R/G mana) and fill the gy via cleanup discards.
      Types among others: artifact+creature (Ornithopter), instant (Shock),
      sorcery (Divination), enchantment (Rancor) -> >= 4.
  ESCAPE: with >=3 untapped lands incl. a Swamp, P0 casts Nethergoyf from
      the graveyard via escape ({2}{B}). The exile half of the cost is the
      device under test.

Assertions:
  A1_setup_ok        pre_escape.json: Nethergoyf in P0 gy; >=4 card types
                     among OTHER P0 gy cards; P0 has >=3 untapped lands
                     incl. a Swamp; P0 priority in own main phase.
  A2_escape_accepted The escape cast is accepted: Nethergoyf leaves the gy
                     (to stack, then battlefield), {2}{B} paid.
  A3_choice_offered  During the exile-cost step the engine offers a per-card
                     choice (vi opportunity or SelectCards with individual
                     candidates) instead of exiling all other gy cards with
                     no decision point.
  A4_subset_accepted A submitted subset covering >=4 card types
                     (Ornithopter+Shock+Divination+Bears) is accepted and
                     the cast completes. (Negative probe first: a 1-card
                     subset must be REJECTED per the "four or more card
                     types" rule; acceptance of it is a related failure.)
  A5_unchosen_remain >=1 non-chosen card remains in P0's graveyard after
                     the escape completes.
  A6_pt_recomputed   Nethergoyf on the battlefield has P/T equal to the
                     number of card types among cards in P0's remaining
                     graveyard (power) and +1 (toughness).
  A7_cleanup         Game at Priority, stack empty, no stall watchdog fired.

Verdict rule: reproduced iff the engine exiles all other gy cards without
offering a choice (A3 fails), or rejects a >=4-type subset / accepts a
<4-type subset (A4 fails), or unchosen cards do not remain (A5 fails).
not-reproduced iff all of A1..A7 pass. blocked iff the fixture can never
be assembled (no Nethergoyf in gy with >=4 types among others and mana).

Evidence: evidence/6609/<run-id>/pre_escape.json, post_escape.json,
run.json, manifest.sha256, summary.png, scenario_6609.py, wire_log.jsonl,
scenario_run.log
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
RUN_ID = "20260910-6609"
EVDIR = f"{BACKFILL}/evidence/6609/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

GOYF = "nethergoyf"
ORNITHOPTER = "ornithopter"
RANCOR = "rancor"
SHOCK = "shock"
DIVINATION = "divination"
BEARS = "grizzly bears"
BOLT = "lightning bolt"
SWAMP = "swamp"
ISLAND = "island"
MOUNTAIN = "mountain"
FOREST = "forest"

# Filtered StateUpdate views omit type_line (observed None in protocol 68),
# so battlefield/land/card-type lookups are name-based. Graveyard card types
# come from the pinned card data (authoritative, matches the engine's data).
LAND_NAMES = {SWAMP, ISLAND, MOUNTAIN, FOREST}
KNOWN_CREATURES = {GOYF, BEARS, ORNITHOPTER}

_CARDDATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.78.0/data/card-data.json"))
_NAME_TYPES = {}
for _k, _c in _CARDDATA.items():
    _ct = (_c.get("card_type") or {})
    _NAME_TYPES[_k] = {str(t).lower()
                       for t in (_ct.get("core_types") or [])}
del _k, _c, _ct


def type_set_of_name(nm):
    return set(_NAME_TYPES.get(nm, ()))

P0_DECK = [(GOYF, 8), (ORNITHOPTER, 8), (RANCOR, 8), (SHOCK, 8),
           (DIVINATION, 8), (BEARS, 8),
           (SWAMP, 8), (ISLAND, 8)]
P1_DECK = [(BOLT, 12), (MOUNTAIN, 36)]

# card types that can appear in a graveyard (per Nethergoyf rulings)
CARD_TYPES = ["artifact", "battle", "creature", "enchantment", "instant",
              "kindred", "land", "planeswalker", "sorcery"]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-10",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/)",
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


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def gy_objs(state, pid):
    return [(int(o), get_obj(state, o))
            for o in player_of(state, pid).get("graveyard", [])]


def gy_names(state, pid):
    return [obj_name(o) for _, o in gy_objs(state, pid)]


def type_set_of(o):
    # Prefer the live type_line, fall back to pinned card data by name.
    tl = str(o.get("type_line") or "").lower()
    if tl and tl != "none":
        return {t for t in CARD_TYPES if t in tl}
    return type_set_of_name(obj_name(o))


def gy_type_set(state, pid, exclude_names=()):
    out = set()
    for oid, o in gy_objs(state, pid):
        if obj_name(o) in exclude_names:
            continue
        out |= type_set_of(o)
    return out


def goyf_gy_oid(state, pid=0):
    for oid, o in gy_objs(state, pid):
        if obj_name(o) == GOYF:
            return oid
    return None


def bf_creatures(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            tl = str(o.get("type_line") or "").lower()
            nm = obj_name(o)
            # filtered views may omit type_line; fall back to known names
            if "creature" in tl or nm in KNOWN_CREATURES:
                if name is None or nm == name:
                    out.append(int(oid))
    return out


def goyf_bf_oid(state, pid=0):
    cs = bf_creatures(state, pid, GOYF)
    return cs[0] if cs else None


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped") and obj_name(o) in LAND_NAMES
                and (name is None or obj_name(o) == name)):
            out.append(int(oid))
    return out


def spell_in_hand_oid(state, pid, name):
    for o in player_of(state, pid).get("hand", []):
        if lname(state, o) == name:
            return int(o)
    return None


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
    if "data" in a:
        msg = dict(msg)
        d = dict(a["data"])
        d.pop("_src_oid", None)
        msg["data"] = d
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("text")):
                t = d.get("name") or d.get("text")
                break
    return str(t)


def choice_status(ch):
    return (ch.get("status") or {}).get("type")


def cand_ref_oid(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("reference") is not None:
            ref = d["reference"]
            if isinstance(ref, dict):
                return ref.get("object_id") or ref.get("id")
            try:
                return int(ref)
            except (TypeError, ValueError):
                return None
    return None


def cand_seat(ch):
    for s in ch.get("surfaces", []) or []:
        d = (s.get("data") or {})
        if isinstance(d, dict) and d.get("seat") is not None:
            try:
                return int(d["seat"])
            except (TypeError, ValueError):
                return None
    return None

async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_escape_accepted", "A3_choice_offered",
            "A4_subset_accepted", "A5_unchosen_remain", "A6_pt_recomputed",
            "A7_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")

    kept = {}
    obs = {
        "phase": "setup",        # setup -> escape -> done
        "answered_ids": [],
        "rejections": [],
        "escape_decisions": [],
        "goyf_cast": False,      # initial battlefield cast
        "div_cast": False,
        "ornithopter_cast": False,
        "shock_cast": False,
        "bears_cast": False,
        "escape_initiated": False,
        "escape_cast_accepted": False,   # goyf left gy for the stack
        "exile_prompt_seen": False,      # per-card choice offered
        "exile_prompt_kind": None,       # "vi" | "action" | None
        "invalid_subset_tried": False,   # 1-card subset negative probe
        "invalid_subset_rejected": None,
        "valid_subset_submitted": False,
        "valid_subset_accepted": None,
        "chosen_names": [],
        "unchosen_names": [],
        "auto_exile_observed": False,    # gy swept with no decision point
        "stuck_watch_fired": False,
        "done": False,
    }

    async def drain_rejections(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                obs["rejections"].append({"who": c.name, "type": t,
                                          "data": data})
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", {"who": c.name, "type": t, "data": data})

    def get_vi(st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    def opp_candidates(opp):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        return (data.get("choices") or data.get("candidates") or [],
                resp.get("type"), data.get("spec"))

    def log_vi(c, st, tag):
        vi = st.get("viewer_interaction") or {}
        opps = vi.get("opportunities") or []
        if not opps:
            return
        for opp in opps:
            choices, rtype, spec = opp_candidates(opp)
            stype = spec.get("type") if isinstance(spec, dict) else spec
            info = {
                "tag": tag, "who": c.name, "canSubmit": vi.get("canSubmit"),
                "interactionId": opp.get("interactionId"),
                "opp_tag": opp.get("tag"),
                "rtype": rtype, "spec_type": stype,
                "prompt": str(opp.get("prompt") or opp.get("title") or "")[:160],
                "n_choices": len(choices),
                "choices": [
                    {"id": ch.get("id"), "text": choice_text(ch)[:80],
                     "status": choice_status(ch),
                     "ref_oid": cand_ref_oid(ch), "seat": cand_seat(ch)}
                    for ch in choices[:16]],
            }
            wire("vi_opportunity", info)
            say(f"[vi {tag}/{c.name}] rtype={rtype} spec={stype} "
                f"prompt={info['prompt'][:70]!r} "
                f"choices={[(x['text'], x['status']) for x in info['choices']][:8]}")

    async def submit_vi_choice(c, opp, choice_ids, why, rtype=None, stype=None):
        """Submit a multi-choice vi response (schema path)."""
        iid = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        rtype = rtype or resp.get("type")
        spec = data.get("spec") or {}
        stype = stype or (spec.get("type") if isinstance(spec, dict) else spec)
        if isinstance(stype, str):
            sub_type = stype
        elif rtype == "exactChoices":
            sub_type = "choose"
        else:
            sub_type = "sequence"
        if sub_type == "choose":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": choice_ids[0]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": sub_type,
                                "data": {"choiceIds": choice_ids}}}
        obs["answered_ids"].append(iid)
        obs["escape_decisions"].append(
            {"interactionId": iid, "why": why, "sub_type": sub_type,
             "choice_ids": choice_ids})
        say(f"[{c.name}] {why}: submit {sub_type} ids={choice_ids} "
            f"(rtype={rtype} spec={stype})")
        wire("decision_submission", {"who": c.name, "why": why,
                                     "interactionId": iid,
                                     "submission": sub})
        await c.send_interaction(sub)

    def gy_choice_names(state, pid=0):
        """Names of cards in P0's graveyard excluding Nethergoyf itself."""
        return [n for n in gy_names(state, pid) if n != GOYF]

    def wanted_subset_ids(state, opp, wanted):
        """Map wanted card names to candidate ids in this opportunity."""
        choices, _rtype, _spec = opp_candidates(opp)
        by_name = {}
        for ch in choices:
            t = choice_text(ch).lower()
            oid = cand_ref_oid(ch)
            name = lname(state, oid).lower() if oid else t
            by_name.setdefault(name, ch.get("id"))
            by_name.setdefault(t, ch.get("id"))
        ids = []
        for w in wanted:
            cid = by_name.get(w)
            if cid is not None:
                ids.append(cid)
        return ids, by_name

    async def handle_exile_vi(c, st, state, wtype):
        """Answer the escape exile-cost choice prompt if present.

        Returns True if a submission was sent. Records whether a genuine
        per-card choice was offered (A3) and drives the invalid-then-valid
        subset probes (A4).

        NOTE (protocol 68): opportunities carry NO usable tag field
        (opp.get("tag") is None). The cleanup discard is detected from the
        authoritative waiting_for.type instead.
        """
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                # Re-answer allowance: after the invalid subset is rejected
                # the same prompt may stay open under the same id; the valid
                # subset still needs to go out.
                if not (obs["invalid_subset_rejected"]
                        and not obs["valid_subset_submitted"]
                        and obs["escape_live"]):
                    continue
            # cleanup discard is always safe to answer; detected via
            # waiting_for.type (protocol 68 has no opp tags).
            if wtype == "DiscardToHandSize":
                ch, why = decide_discard(opp, state, c.name)
                if ch is not None:
                    obs["_discard_ids"] = [x.get("id") for x in ch]
                    await submit_vi_choice(c, opp, obs["_discard_ids"],
                                           why, stype="select")
                    acted = True
                continue
            if not obs["escape_live"]:
                continue
            choices, rtype, spec = opp_candidates(opp)
            if not choices:
                continue
            blob = json.dumps(opp, default=str).lower()
            # Heuristic: a per-card exile choice references graveyard cards
            # of P0 (seat 0) or names exile/graveyard in the prompt.
            gy_names_l = set(gy_choice_names(state, 0))
            refs_gy = False
            for ch in choices:
                oid = cand_ref_oid(ch)
                if oid and lname(state, oid).lower() in gy_names_l:
                    refs_gy = True
                    break
                if choice_text(ch).lower() in gy_names_l:
                    refs_gy = True
                    break
            mentions = ("exile" in blob and "graveyard" in blob)
            if not (refs_gy or mentions):
                continue
            # This is the exile-cost choice prompt.
            if not obs["exile_prompt_seen"]:
                obs["exile_prompt_seen"] = True
                obs["exile_prompt_kind"] = "vi"
                say(f"[{c.name}] EXILE CHOICE PROMPT SEEN (vi): "
                    f"rtype={rtype} n={len(choices)}")
                wire("exile_prompt", {"kind": "vi", "opp": opp,
                                      "gy_names": sorted(gy_names_l)})
            stype = spec.get("type") if isinstance(spec, dict) else spec
            say(f"[{c.name}] exile prompt spec={stype!r} rtype={rtype!r}")
            wire("exile_prompt_detail",
                 {"rtype": rtype, "spec": spec,
                  "n_choices": len(choices),
                  "min": (spec.get("data") or {}).get("min")
                  if isinstance(spec, dict) else None,
                  "max": (spec.get("data") or {}).get("max")
                  if isinstance(spec, dict) else None})
            if not obs["invalid_subset_tried"]:
                # Negative probe: a single card covers 1 type < 4 -> the
                # engine MUST reject it per the card's "four or more card
                # types among them" rule.
                ids, _by = wanted_subset_ids(state, opp, [SHOCK])
                if not ids:
                    # fall back to the first available candidate
                    avail = [ch for ch in choices
                             if choice_status(ch) in ("available", "Available", None)]
                    ids = [(avail[0] if avail else choices[0]).get("id")]
                obs["invalid_subset_tried"] = True
                obs["invalid_subset_ids"] = ids
                await submit_vi_choice(c, opp, ids, "invalid_subset_1card",
                                       rtype=rtype, stype=stype)
                acted = True
                continue
            if obs["invalid_subset_rejected"] and not obs["valid_subset_submitted"]:
                # Valid probe: one card per type -> 4 types among them.
                wanted = [ORNITHOPTER, SHOCK, DIVINATION, BEARS]
                ids, by_name = wanted_subset_ids(state, opp, wanted)
                if len(ids) < 4:
                    say(f"[{c.name}] WARNING: only mapped {len(ids)}/4 "
                        f"wanted cards; submitting what we have")
                    wire("subset_map_short", {"ids": ids})
                obs["valid_subset_submitted"] = True
                obs["valid_subset_ids"] = ids
                rev = {v: k for k, v in by_name.items()}
                obs["chosen_names"] = [rev.get(i, "?") for i in ids]
                await submit_vi_choice(c, opp, ids, "valid_subset_4types",
                                       rtype=rtype, stype=stype)
                acted = True
                continue
            say(f"[{c.name}] exile prompt already probed; leaving unanswered")
            wire("exile_prompt_repeat", {"interactionId": iid})
        return acted

    def decide_discard(opp, state, who):
        # P0's graveyard is filled deliberately through discards: one of
        # each type-card first (they add new card types), then dead lands,
        # then anything except a Goyf we still need to cast.
        choices, _rtype, _spec = opp_candidates(opp)
        avail = [ch for ch in choices
                 if choice_status(ch) in ("available", "Available", None)]
        if not avail:
            avail = choices
        goyf_in_gy = goyf_gy_oid(state, 0) is not None
        gy = set(gy_names(state, 0))

        def rank(ch):
            t = choice_text(ch).lower()
            oid = cand_ref_oid(ch)
            nm = lname(state, oid).lower() if oid else t
            if nm == GOYF:
                return 0 if goyf_in_gy else 9   # keep a goyf to cast
            if nm in (ORNITHOPTER, SHOCK, DIVINATION, BEARS, RANCOR) \
                    and nm not in gy:
                return 1    # first copy -> gy, adds a card type
            if nm in (SWAMP, ISLAND, MOUNTAIN, FOREST):
                return 3    # lands in hand are dead at cleanup
            return 5
        avail.sort(key=rank)
        n_discard = max(1, len(avail) - 7)
        return avail[:n_discard], "discard_to_hand_size"

    obs["escape_live"] = False

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

    def fixture_ready(state):
        if goyf_gy_oid(state, 0) is None:
            return False
        if len(gy_type_set(state, 0, exclude_names=(GOYF,))) < 4:
            return False
        lands = untapped_lands(state, 0)
        if len(lands) < 3:
            return False
        if not untapped_lands(state, 0, SWAMP):
            return False
        return True

    def log_castspell_actions(state, acts):
        for a in acts:
            if a["type"] != "CastSpell":
                continue
            d = a.get("data", {})
            oid = d.get("object_id")
            o = get_obj(state, oid) if oid is not None else {}
            wire("castspell_action",
                 {"object_id": oid, "name": obj_name(o),
                  "zone": o.get("zone"), "controller": o.get("controller"),
                  "data_keys": sorted(d.keys()),
                  "data": {k: v for k, v in d.items()
                           if k not in ("object_id",)}})
            say(f"[P0] CastSpell offered: {obj_name(o)} zone={o.get('zone')} "
                f"data_keys={sorted(d.keys())}")

    async def answer_target_prompt(c, st, state, want_oid=None, want_seat=None,
                                   clear_key=None):
        """Answer a target-selection opportunity for a pending cast.

        Returns True if a submission was sent. Only schema-type opportunities
        are treated as target prompts (AGENTS.md)."""
        vi = get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in obs["answered_ids"]:
                continue
            choices, rtype, spec = opp_candidates(opp)
            if not choices:
                continue
            stype = spec.get("type") if isinstance(spec, dict) else spec
            # Only treat genuine target-selection schemas as target prompts.
            # Priority menus (exactChoices without a pending cast) must never
            # be answered here.
            if not (stype == "sequence" or rtype == "exactChoices"):
                continue
            pick = None
            for ch in choices:
                if want_oid is not None and cand_ref_oid(ch) == want_oid:
                    pick = ch
                    break
                if want_seat is not None and cand_seat(ch) == want_seat:
                    pick = ch
                    break
            if pick is None:
                continue
            wire("target_prompt_answered",
                 {"who": c.name, "interactionId": iid, "want_oid": want_oid,
                  "want_seat": want_seat,
                  "pick": choice_text(pick)[:60]})
            await submit_vi_choice(c, opp, [pick.get("id")],
                                   f"target_{clear_key or 'pick'}",
                                   rtype=rtype, stype=stype)
            if clear_key:
                obs[clear_key] = False
            acted = True
        return acted

    async def mulligan_tick(c, st, acts, state, who, keep_fn, max_mulls=3):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(who):
            hn = hand_names(state, c.player_id)
            mulls = kept.get(who + "_mulls", 0)
            if keep_fn(hn) or mulls >= max_mulls:
                kept[who] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{who} keeps (hand={hn[:8]}, mulls={mulls})")
            else:
                kept[who + "_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{who} mulligans #{mulls + 1} (hand={hn[:8]})")
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(who + "_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == c.player_id:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for o in
                            player_of(state, c.player_id).get("hand", [])]

                def bkey(oid):
                    nm = lname(state, oid)
                    # bottom non-lands first (keep the hunted lands!);
                    # keep goyf most of all
                    if nm == GOYF:
                        return 2
                    if nm in (SWAMP, ISLAND, MOUNTAIN, FOREST):
                        return 1
                    return 0
                picks = sorted(hand_ids, key=bkey)[:count]
                kept[who + "_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{who} bottoms {count}")
            return True
        return False

    def play_land_action(acts, state):
        cands = [a for a in acts if a["type"] == "PlayLand"]
        if not cands:
            return None
        if not obs.get("playland_logged"):
            obs["playland_logged"] = True
            for a in cands:
                d = a.get("data", {})
                wire("playland_action",
                     {"object_id": d.get("object_id"),
                      "name": lname(state, d.get("object_id")),
                      "data_keys": sorted(d.keys())})
        # figure out which lands are on the battlefield via objects
        # (name-based: filtered views omit type_line)
        have = set()
        for _oid, o in state.get("objects", {}).items():
            if (o.get("zone") == "Battlefield" and o.get("controller") == 0
                    and obj_name(o) in LAND_NAMES):
                have.add(obj_name(o))
        for want in (SWAMP, ISLAND):
            if want not in have:
                for a in cands:
                    d = a.get("data", {})
                    if lname(state, d.get("object_id")) == want:
                        return a
        return cands[0]

    async def p0_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(p0, st, acts, state, "P0",
                               lambda hn: (
                                   sum(1 for n in hn if n in LAND_NAMES) >= 2
                                   and SWAMP in hn
                                   and any(n in (GOYF, DIVINATION, ORNITHOPTER)
                                           for n in hn)),
                               max_mulls=4):
            return True

        # payments first (land taps for mana)
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return True

        # exile-choice / discard opportunities
        if await handle_exile_vi(p0, st, state, wtype):
            return True

        # target prompt for a pending Shock cast (unused in current plan;
        # Shock is uncastable with no red mana and fills the gy via discard)
        if obs.get("p0_shock_pending"):
            if await answer_target_prompt(p0, st, state, want_seat=1,
                                          clear_key="p0_shock_pending"):
                return True
            obs["p0_shock_pending"] = False

        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p0.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers",
                                      "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return True

        # ---- escape progress tracking ----
        if obs["escape_live"]:
            # invalid-subset rejection detection
            if (obs["invalid_subset_tried"]
                    and obs["invalid_subset_rejected"] is None
                    and len(obs["rejections"]) > obs.get("_rej_mark", 0)):
                new_rej = obs["rejections"][obs.get("_rej_mark", 0):]
                obs["invalid_subset_rejected"] = True
                say(f"[P0] invalid 1-card subset REJECTED: "
                    f"{json.dumps(new_rej)[:500]}")
                wire("invalid_subset_rejected", new_rej)
            # escape cast accepted: goyf left the graveyard
            if (obs["escape_initiated"] and not obs["escape_cast_accepted"]
                    and goyf_gy_oid(state, 0) is None):
                obs["escape_cast_accepted"] = True
                say("[P0] escape cast accepted: Nethergoyf left the graveyard")
                wire("escape_accepted", {})
            # auto-exile detection: all other gy cards gone with no prompt
            if (obs["escape_cast_accepted"]
                    and not obs["exile_prompt_seen"]
                    and not obs["auto_exile_observed"]):
                if not gy_choice_names(state, 0):
                    obs["auto_exile_observed"] = True
                    say("[P0] AUTO-EXILE OBSERVED: graveyard swept with no "
                        "choice prompt (the reported bug)")
                    wire("auto_exile", {"gy": gy_names(state, 0)})
            # invalid subset accepted (constraint missing): cast completed
            # without ever submitting the valid set
            if (obs["invalid_subset_tried"]
                    and not obs["valid_subset_submitted"]
                    and goyf_bf_oid(state, 0) is not None):
                obs["invalid_subset_accepted"] = True
                say("[P0] INVALID 1-card subset was ACCEPTED (missing 4-type "
                    "constraint) - related failure")
                wire("invalid_subset_accepted", {})
            # valid subset accepted: goyf on battlefield, chosen exiled
            if (obs["valid_subset_submitted"]
                    and obs["valid_subset_accepted"] is None
                    and goyf_bf_oid(state, 0) is not None):
                obs["valid_subset_accepted"] = True
                say("[P0] valid 4-type subset ACCEPTED; Nethergoyf escaped "
                    "to the battlefield")
                wire("valid_subset_accepted", {})
            # done: goyf on the battlefield and stack settled
            if (goyf_bf_oid(state, 0) is not None
                    and not (state.get("stack") or [])
                    and not obs["done"]):
                obs["done"] = True
                obs["phase"] = "done"
                obs["escape_live"] = False
                await export("post_escape")
                say("[P0] ESCAPE COMPLETE: Nethergoyf on battlefield; "
                    "exported post_escape.json")
                return True

        if not ((wtype == "Priority") and state.get("priority_player") == 0):
            return False

        own_main = (state.get("active_player") == 0
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            # 1. land drop
            pla = play_land_action(acts, state)
            if pla:
                await submit_as_is(p0, pla)
                say(f"P0 plays land "
                    f"{lname(state, pla.get('data', {}).get('object_id'))}")
                return True
            # 2. escape initiation
            if obs["phase"] == "setup" and fixture_ready(state):
                log_castspell_actions(state, acts)
                gid = goyf_gy_oid(state, 0)
                target = None
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and int(d.get("object_id", -1)) == gid):
                        target = a
                        break
                if target is None:
                    if not obs.get("no_escape_cast_logged"):
                        obs["no_escape_cast_logged"] = True
                        say("[P0] FIXTURE READY but no CastSpell offered for "
                            "the graveyard Nethergoyf")
                        wire("no_escape_cast",
                             {"goyf_gy_oid": gid,
                              "gy_types": sorted(gy_type_set(
                                  state, 0, exclude_names=(GOYF,)))})
                    return False
                await export("pre_escape")
                obs["_rej_mark"] = len(obs["rejections"])
                obs["phase"] = "escape"
                obs["escape_initiated"] = True
                obs["escape_live"] = True
                say(f"[P0] initiating ESCAPE of Nethergoyf (gy oid {gid})")
                wire("escape_initiated", target)
                await submit_as_is(p0, target)
                return True
            # 3. setup casts: Goyf ({1}{B}), Ornithopter (free),
            # Divination ({2}{U}). Shock/Bears/Rancor are uncastable here
            # (no R/G mana) and reach the graveyard via cleanup discards.
            n_untapped = len(untapped_lands(state, 0))
            has_swamp = bool(untapped_lands(state, 0, SWAMP))
            has_island = bool(untapped_lands(state, 0, ISLAND))

            def try_cast(name, cond, flag):
                if obs[flag] or not cond:
                    return None
                sid = spell_in_hand_oid(state, 0, name)
                if sid is None:
                    return None
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and int(d.get("object_id", -1)) == sid):
                        return a
                return None

            a = try_cast(GOYF, n_untapped >= 2 and has_swamp
                         and goyf_gy_oid(state, 0) is None
                         and goyf_bf_oid(state, 0) is None, "goyf_cast")
            if a:
                obs["goyf_cast"] = True
                say("P0 casts Nethergoyf")
                wire("cast_goyf", a)
                await submit_as_is(p0, a)
                return True
            a = try_cast(ORNITHOPTER, True, "ornithopter_cast")
            if a:
                obs["ornithopter_cast"] = True
                say("P0 casts Ornithopter")
                await submit_as_is(p0, a)
                return True
            a = try_cast(DIVINATION, n_untapped >= 3 and has_island,
                         "div_cast")
            if a:
                # keep casting Divination while able: each cast draws 2
                # (overfilling the hand for discards) and puts a sorcery
                # in the graveyard.
                say("P0 casts Divination")
                wire("cast_divination", a)
                await submit_as_is(p0, a)
                return True

        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return True
        return False

    async def p1_tick(st, acts, state):
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(p1, st, acts, state, "P1",
                               lambda hn: MOUNTAIN in hn):
            return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return True
        if await handle_exile_vi(p1, st, state, wtype):
            return True
        if obs.get("p1_bolt_pending"):
            want = obs.get("p1_bolt_target")
            if await answer_target_prompt(p1, st, state, want_oid=want,
                                          clear_key="p1_bolt_pending"):
                return True
            obs["p1_bolt_pending"] = False
        if wtype == "DeclareAttackers":
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await p1.send_action({"type": "DeclareAttackers", "data": d})
            return True
        if wtype == "DeclareBlockers":
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers",
                                      "data": d})
            return True
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return True
        if wtype != "Priority" or state.get("priority_player") != 1:
            return False
        own_main = (state.get("active_player") == 1
                    and state.get("phase") in ("PreCombatMain", "PostCombatMain"))
        if own_main:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(p1, a)
                    say(f"P1 plays land "
                        f"{lname(state, a.get('data', {}).get('object_id'))}")
                    return True
            # bolt P0's creatures: goyf > bears > ornithopter (name-based:
            # filtered views omit type_line)
            target = None
            for nm in (GOYF, BEARS, ORNITHOPTER):
                cs = bf_creatures(state, 0, nm)
                if cs:
                    target = (nm, cs[0])
                    break
            n_mtn = len(untapped_lands(state, 1, MOUNTAIN))
            bid = spell_in_hand_oid(state, 1, BOLT)
            if target is not None:
                say(f"[P1] bolt diag: target={target[0]} oid={target[1]} "
                    f"untapped_mtn={n_mtn} bolt_in_hand={bid is not None}")
                wire("p1_bolt_diag",
                     {"target": target[0], "target_oid": target[1],
                      "untapped_mtn": n_mtn, "bolt_in_hand": bid is not None,
                      "hand": hand_names(state, 1)[:10]})
            if target is not None and n_mtn > 0 and bid is not None:
                for a in acts:
                    d = a.get("data", {})
                    if (a["type"] == "CastSpell"
                            and int(d.get("object_id", -1)) == bid):
                        obs["p1_bolt_pending"] = True
                        obs["p1_bolt_target"] = target[1]
                        say(f"P1 bolts {target[0]} (oid {target[1]})")
                        wire("cast_bolt", {"action": a, "target": target[1]})
                        await submit_as_is(p1, a)
                        return True
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
                return True
        return False

    def evaluate():
        pre = load_env("pre_escape")
        post = load_env("post_escape")
        notes.append(
            f"escape_initiated={obs['escape_initiated']} "
            f"escape_cast_accepted={obs['escape_cast_accepted']} "
            f"exile_prompt_seen={obs['exile_prompt_seen']} "
            f"({obs['exile_prompt_kind']}) "
            f"invalid_tried={obs['invalid_subset_tried']} "
            f"invalid_rejected={obs['invalid_subset_rejected']} "
            f"invalid_accepted={obs.get('invalid_subset_accepted')} "
            f"valid_submitted={obs['valid_subset_submitted']} "
            f"valid_accepted={obs['valid_subset_accepted']} "
            f"auto_exile={obs['auto_exile_observed']} "
            f"rejections={len(obs['rejections'])}")
        # A1
        if pre is not None:
            s = pre["state"]
            goyf_gy = goyf_gy_oid(s, 0) is not None
            types = gy_type_set(s, 0, exclude_names=(GOYF,))
            lands = untapped_lands(s, 0)
            ok = (goyf_gy and len(types) >= 4 and len(lands) >= 3
                  and bool(untapped_lands(s, 0, SWAMP))
                  and s.get("active_player") == 0
                  and s.get("phase") in ("PreCombatMain", "PostCombatMain")
                  and s.get("priority_player") == 0)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: goyf_in_gy={goyf_gy}, types_among_others="
                         f"{sorted(types)} ({len(types)}), untapped_lands="
                         f"{len(lands)}, swamp_untapped="
                         f"{bool(untapped_lands(s, 0, SWAMP))}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre_escape.json missing (fixture never ready)")
        # A2
        if post is not None:
            s = post["state"]
            goyf_bf = goyf_bf_oid(s, 0) is not None
            goyf_gy = goyf_gy_oid(s, 0) is not None
            ok = goyf_bf and not goyf_gy
            ass["A2_escape_accepted"] = "passed" if ok else "failed"
            notes.append(f"A2: goyf_on_bf={goyf_bf}, goyf_in_gy={goyf_gy}")
        else:
            ass["A2_escape_accepted"] = "failed"
            notes.append("A2: post_escape.json missing")
        # A3
        if obs["exile_prompt_seen"]:
            ass["A3_choice_offered"] = "passed"
            notes.append(f"A3: per-card exile choice offered via "
                         f"{obs['exile_prompt_kind']}")
        elif obs["auto_exile_observed"]:
            ass["A3_choice_offered"] = "failed"
            notes.append("A3: NO choice offered; graveyard swept with no "
                         "decision point (the reported bug)")
        else:
            ass["A3_choice_offered"] = "failed"
            notes.append("A3: no per-card exile choice observed")
        # A4
        if obs.get("invalid_subset_accepted"):
            ass["A4_subset_accepted"] = "failed"
            notes.append("A4: RELATED FAILURE - a 1-card subset (<4 types) "
                         "was ACCEPTED; the 4-type aggregate constraint is "
                         "not enforced")
        elif obs["valid_subset_accepted"]:
            ass["A4_subset_accepted"] = "passed"
            notes.append("A4: 1-card subset rejected "
                         f"(rejected={obs['invalid_subset_rejected']}); "
                         "4-type subset accepted and cast completed")
        elif obs["invalid_subset_tried"] and obs["invalid_subset_rejected"]:
            ass["A4_subset_accepted"] = "failed"
            notes.append("A4: 1-card subset correctly rejected, but the "
                         "4-type subset was never accepted / cast did not "
                         "complete")
        else:
            ass["A4_subset_accepted"] = "not-run"
            notes.append("A4: no exile choice prompt to probe")
        # A5 / A6 need the post state and the chosen set
        chosen = set(obs.get("chosen_names", []))
        if post is not None:
            s = post["state"]
            gy = [o for _, o in gy_objs(s, 0)]
            unchosen = [o for o in gy
                        if obj_name(o) not in chosen
                        and obj_name(o) != GOYF]
            ok = len(unchosen) >= 1
            ass["A5_unchosen_remain"] = "passed" if ok else "failed"
            notes.append(f"A5: unchosen remaining in gy="
                         f"{[obj_name(o) for o in unchosen]}")
            # A6
            goyf_oid = goyf_bf_oid(s, 0)
            if goyf_oid is not None:
                g = get_obj(s, goyf_oid)
                types = gy_type_set(s, 0)
                pw = g.get("power")
                tw = g.get("toughness")
                pwr = pw.get("value") if isinstance(pw, dict) else pw
                tgh = tw.get("value") if isinstance(tw, dict) else tw
                try:
                    pwr_i, tgh_i = int(pwr), int(tgh)
                except (TypeError, ValueError):
                    pwr_i = tgh_i = None
                ok = (pwr_i == len(types) and tgh_i == len(types) + 1)
                ass["A6_pt_recomputed"] = "passed" if ok else "failed"
                notes.append(f"A6: goyf P/T={pwr_i}/{tgh_i}, types in "
                             f"remaining gy={sorted(types)} "
                             f"(want {len(types)}/{len(types) + 1})")
            else:
                ass["A6_pt_recomputed"] = "failed"
                notes.append("A6: Nethergoyf not on battlefield")
            # A7
            wf = (s.get("waiting_for") or {}).get("type")
            ok = (not (s.get("stack") or []) and wf == "Priority"
                  and not obs["stuck_watch_fired"])
            ass["A7_cleanup"] = "passed" if ok else "failed"
            notes.append(f"A7: stack_empty={not (s.get('stack') or [])}, "
                         f"wf={wf}, stuck_watch={obs['stuck_watch_fired']}")
        else:
            for k in ("A5_unchosen_remain", "A6_pt_recomputed", "A7_cleanup"):
                ass[k] = "failed"
            notes.append("A5/A6/A7: post_escape.json missing")
        if ass["A1_setup_ok"] != "passed":
            verdict = "blocked"
            notes.append("fixture never assembled; no trustworthy result")
        elif all(ass[k] == "passed" for k in ass):
            verdict = "not-reproduced"
        else:
            verdict = "reproduced"
            notes.append("the Nethergoyf escape exile-cost path did not "
                         "behave per the oracle text on this build")
        return verdict

    async def finish():
        dur = time.time() - t_start
        if not os.path.exists(f"{EVDIR}/post_escape.json"):
            try:
                await export("post_escape")
            except Exception:
                pass
        verdict = evaluate()
        run = {
            "issue": 6609,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_6609.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": {k: v for k, v in obs.items()
                             if k not in ("escape_decisions",)},
            "escape_decisions": obs["escape_decisions"],
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x spell density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "The escape fixture is reconstructed (cast + Bolt), not the "
                "reporter's attached game state; card in graveyard via "
                "discard would be an equivalent path.",
                "The prebuilt server has no standalone state-restore; states "
                "are authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x Nethergoyf/Ornithopter/Rancor/Shock/Divination/"
                          "Grizzly Bears + 8x Swamp + 8x Island; "
                          "P1: 12x Lightning Bolt + 36x Mountain",
            "contract_line": "Escape Nethergoyf ({2}{B}): engine must offer a "
                             "per-card exile choice; a 1-card (<4 types) subset "
                             "must be rejected; a 4-type subset must be accepted; "
                             "unchosen cards remain; P/T recomputes from the "
                             "remaining graveyard",
        }
        with open(f"{EVDIR}/run.json", "w") as f:
            json.dump(run, f, indent=1)
        try:
            WIRE.close()
        except Exception:
            pass
        try:
            RUNLOG.close()
        except Exception:
            pass
        print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)

    with open(f"{EVDIR}/scenario_6609.py", "w") as f:
        f.write(open(f"{BACKFILL}/driver/scenario_6609.py").read())

    t0 = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    stuck_watch = None
    while time.time() - t0 < 1500:
        await asyncio.sleep(0.15)
        for c, tick in ((p0, p0_tick), (p1, p1_tick)):
            await drain_rejections(c)
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
            if c.name == "P0":
                wf_now = ((st.get("state", {}) or {}).get("waiting_for")
                          or {}).get("type")
                if obs["escape_live"] or (wf_now and wf_now != "Priority"):
                    log_vi(c, st, "esc" if obs["escape_live"]
                           else f"wf-{wf_now}")
        if obs["done"]:
            say("escape complete; finishing")
            await finish()
            return
        if p0.latest and p0.latest["state"].get("turn_number", 0) >= 22 \
                and not obs["done"]:
            notes.append("turn 22 reached without completing the escape; "
                         "bailing out to evaluation")
            say("turn 22 bail-out; finishing")
            await finish()
            return
        if obs["escape_live"] and stuck_watch is None:
            stuck_watch = time.time() + 180
        if not obs["escape_live"]:
            stuck_watch = None
        if stuck_watch and time.time() > stuck_watch:
            s = p0.latest["state"] if p0.latest else {}
            wf = (s.get("waiting_for") or {}).get("type")
            obs["stuck_watch_fired"] = True
            notes.append(f"STUCK WATCH FIRED (180s mid-escape): waiting_for={wf} "
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
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} hand={hand_names(s, 0)[:6]} "
                f"gy0={[obj_name(o) for _, o in gy_objs(s, 0)][:10]} "
                f"goyf_gy={goyf_gy_oid(s, 0) is not None} "
                f"phase_sm={obs['phase']} escape_live={obs['escape_live']}")
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
