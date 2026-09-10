#!/usr/bin/env python3
"""Issue #5678: Alms Collector -- "would draw two or more cards" antecedent
never parses to a Draw replacement (CR 121.2a instruction-count gap).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Issue (internal triage, 2026-07-12, status:confirmed): Alms Collector never
becomes a ReplacementEvent::Draw definition. The parser's draw-replacement
branch only matches "would draw a card" and "would draw one or more cards";
there is no arm for "would draw two or more cards", so `mentions_draw` is
false and the line never produces a replacement at all. Fix PR #5867 is still
open/unmerged. In the pinned v0.78.0 card-data.json, "alms collector" has
"replacements": [] while "quantum riddler" ("would draw one or more cards")
parses to a Draw replacement (control).

Oracle text (verified from pinned v0.78.0 card-data.json):
  Alms Collector {2}{W}{W} 3/4 Flash creature:
    "Flash
     If an opponent would draw two or more cards, instead you and that player
     each draw a card."

Game: P0 casts Alms Collector, then P1 casts Divination ("Draw two cards.")
during their own main phase. Per Oracle the 2-card draw instruction is
replaced before any individual draw (CR 121.2a): P0 draws 1 and P1 draws 1.
Buggy behavior: no replacement exists, so P1 draws 2 and P0 draws 0.

Assertions:
  A1_setup      pre: Alms Collector on P0 battlefield, Divination in P1 hand,
                P1 has 3+ untapped Islands, both at 20 life, main phase.
  A2_parse      pinned card-data still shows "replacements": [] for Alms
                Collector (the reported gap), while Quantum Riddler parses to
                a Draw replacement (control).
  A3_draw_split pre->post: P0 hand +1 / library -1, P1 hand net 0
                (-1 cast, +1 draw) / library -1, i.e. the 1-1 split.
  A4_cleanup    Divination in P1 graveyard, stack empty, game proceeding.

Verdict rule: reproduced iff A1_setup completed and the in-engine draw
outcome deviates from Oracle (A3_draw_split failed). not-reproduced iff
A3_draw_split passed (the 1-1 split happened in-engine). blocked iff
A1_setup failed.

Evidence: evidence/5678/<run-id>/pre.json, post.json, parse_evidence.json,
run.json, manifest.sha256, summary.png, scenario_5678.py, wire_log.jsonl,
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
RUN_ID = "20260909-5678"
ISSUE = 5678
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

ALMS = "alms collector"
DIV = "divination"
ISLAND = "island"
PLAINS = "plains"
SWAMP = "swamp"  # referenced by the shared Game core; not in either deck

DECK_P0 = [(ALMS, 8), (PLAINS, 26), (ISLAND, 26)]
DECK_P1 = [(DIV, 8), (ISLAND, 52)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-5678",
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


def lname(state, oid):
    return obj_name(state.get("objects", {}).get(str(oid), {}))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def gy_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("graveyard", [])]


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []))


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                and not o.get("tapped") and obj_name(o) in ("island", "swamp"):
            if name is None or obj_name(o) == name:
                out.append(int(oid))
    return out


def on_bf(state, pid, name):
    return any(o.get("zone") == "Battlefield" and o.get("controller") == pid
               and obj_name(o) == name
               for o in state.get("objects", {}).values())


def in_gy(state, pid, name):
    return any(obj_name(state.get("objects", {}).get(str(o), {})) == name
               for o in player_of(state, pid).get("graveyard", []))


def card_in_hand_oid(state, pid, name):
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


def waiting_actor(state):
    wf = state.get("waiting_for") or {}
    d = wf.get("data", {}) or {}
    if isinstance(d.get("player"), int):
        return d["player"]
    for p in d.get("pending", []) or []:
        if isinstance(p.get("player"), int):
            return p["player"]
    return None


def can_act(state, pid):
    a = waiting_actor(state)
    return a is None or a == pid


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


class Game:
    def __init__(self, tag, deck_p0, deck_p1):
        self.tag = tag
        self.deck_p0 = deck_p0
        self.deck_p1 = deck_p1
        self.p0 = self.p1 = None
        self.kept = {}
        self.obs = {"interaction_shapes": [], "notes": []}
        self.submitted = set()
        self.shapes_logged = set()
        self.pre = None          # pre state dict
        self.post = None         # post state dict
        self.pre_done = False
        self.post_done = False
        self.no_action_since = None
        self.stall_recorded = False

    def say(self, *a):
        say(f"[{self.tag}]", *a)

    def note(self, m):
        self.obs["notes"].append(m)

    async def start(self):
        self.p0 = PhaseClient(f"{self.tag}-P0")
        await self.p0.connect()
        await self.p0.create(deck(*self.deck_p0))
        self.p1 = PhaseClient(f"{self.tag}-P1")
        await self.p1.connect()
        await self.p1.join(self.p0.game_code, deck(*self.deck_p1))
        self.say(f"game {self.p0.game_code} P0seat={self.p0.player_id} "
                 f"P1seat={self.p1.player_id}")

    def get_vi(self, st):
        vi = st.get("viewer_interaction") or {}
        return vi if vi.get("canSubmit") else None

    async def scan_interactions(self, st, who, extra=None):
        """Log every opportunity; answer schema target prompts per policy."""
        vi = self.get_vi(st)
        if not vi:
            return False
        acted = False
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            iid = opp.get("interactionId")
            spec = data.get("spec", {}) or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            chs = data.get("choices") or data.get("candidates") or []
            texts = [choice_text(ch) for ch in chs]
            blob = " // ".join(texts)
            key = (who, rtype, stype, blob[:60])
            if key not in self.shapes_logged:
                self.shapes_logged.add(key)
                self.say(f"interaction rtype={rtype} spec={stype} "
                         f"choices=[{blob[:220]}]")
                wire(f"{self.tag}_interaction_shape",
                     {"who": who, "rtype": rtype, "spec": stype,
                      "interaction": opp})
                self.obs["interaction_shapes"].append(
                    {"who": who, "rtype": rtype, "spec": stype,
                     "choices": texts[:12]})
            if iid in self.submitted:
                continue
            if extra:
                sub = extra(opp, rtype, stype, chs)
                if sub:
                    c = self.p0 if who == "P0" else self.p1
                    wire(f"{self.tag}_interaction_submit",
                         {"who": who, "submission": sub, "interaction": opp})
                    await c.send_interaction(sub)
                    self.submitted.add(iid)
                    acted = True
        return acted

    async def export(self, name, client):
        env = await client.export_state()
        with open(f"{EVDIR}/{name}.json", "w") as f:
            f.write(env)
        return json.loads(env)["state"]

    async def discard_down(self, c, pid, state, acts, avoid=()):
        wtype = (state.get("waiting_for") or {}).get("type") or ""
        if "Discard" not in wtype or not can_act(state, pid):
            return False
        sc = find_action(acts, "SelectCards")
        if not sc:
            return False
        hids = hand_ids(state, pid)
        n = max(0, len(hids) - 7)
        d = (state.get("waiting_for") or {}).get("data", {}) or {}
        for k in ("count", "amount", "number"):
            if isinstance(d.get(k), int):
                n = d[k]
        def rank(oid):
            nm = lname(state, oid)
            if nm in avoid:
                return (3, nm)
            if nm in (ISLAND, SWAMP):
                return (0, nm)
            return (1, nm)
        picks = sorted(hids, key=rank)[:n]
        if not picks:
            return False
        sub = {"type": "SelectCards", "data": {"cards": [int(x) for x in picks]}}
        wire(f"{self.tag}_discard", {"pid": pid, "waiting_for": wtype,
                                     "submission": sub})
        await c.send_action(sub)
        self.say(f"P{pid} discards {[lname(state, x) for x in picks]} ({wtype})")
        return True

    async def generic_tick(self, st, acts, state, pid, mull_keep, avoid=()):
        """Shared: mulligan, payments, discards, land, passes, empty combat."""
        c = self.p0 if pid == 0 else self.p1
        wtype = (state.get("waiting_for") or {}).get("type")
        ma = find_action(acts, "MulliganDecision")
        if ma and not self.kept.get(pid):
            mulls = self.kept.get(f"mull_{pid}", 0)
            if mull_keep(state, pid) or mulls >= 3:
                self.kept[pid] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                self.say(f"P{pid} keeps (mulls={mulls})")
            else:
                self.kept[f"mull_{pid}"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                self.say(f"P{pid} mulligans #{mulls + 1}")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not self.kept.get(f"bot_{pid}"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hids = hand_ids(state, pid)
                def bkey(oid):
                    nm = lname(state, oid)
                    return 0 if nm in (ISLAND, SWAMP) else 1
                picks = sorted(hids, key=bkey)[:count]
                self.kept[f"bot_{pid}"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                self.say(f"P{pid} bottoms {count}")
                return True
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return True
        if await self.discard_down(c, pid, state, acts, avoid=avoid):
            return True
        if wtype == "DeclareAttackers" and can_act(state, pid):
            da = find_action(acts, "DeclareAttackers")
            if da:
                d = dict(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": d})
                return True
        if wtype == "DeclareBlockers" and can_act(state, pid):
            da = find_action(acts, "DeclareBlockers")
            if da:
                d = dict(da.get("data", {}))
                d["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": d})
                return True
        if wtype == "OrderTriggers" and can_act(state, pid):
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
                return True
        return False

    def stall_check(self, state):
        """True if the acting seat has no actionable submission right now."""
        wf = state.get("waiting_for") or {}
        wtype = wf.get("type")
        if not wtype or wtype in ("Priority",):
            self.no_action_since = None
            return False
        actor = waiting_actor(state)
        st = None
        # find the acting client's latest state
        c = self.p0 if actor == 0 else self.p1 if actor == 1 else None
        if c is None or not c.latest:
            return False
        st = c.latest
        acts = merged_actions(st)
        vi = self.get_vi(st)
        actionable = bool(acts) or bool(vi and vi.get("opportunities"))
        if actionable:
            self.no_action_since = None
            return False
        if self.no_action_since is None:
            self.no_action_since = time.time()
        return time.time() - self.no_action_since > 60

    async def run_loop(self, tick_p0, tick_p1, done_pred, timeout_s=1500):
        t0 = time.time()
        last, last_tick = {}, {}
        last_diag = 0.0
        while time.time() - t0 < timeout_s:
            await asyncio.sleep(0.15)
            for c, tick in ((self.p0, tick_p0), (self.p1, tick_p1)):
                st = c.latest
                if not st:
                    continue
                rev = c.revision
                if rev == last.get(c.name) and \
                        time.time() - last_tick.get(c.name, 0) <= 5:
                    continue
                last[c.name] = rev
                last_tick[c.name] = time.time()
                try:
                    await tick(st, merged_actions(st), st["state"])
                except Exception as e:
                    self.say(f"tick error {c.name}: {e}")
                    wire(f"{self.tag}_tick_error", {"who": c.name, "err": str(e)})
            if self.post_done and done_pred():
                self.say("post exported and contract complete; finishing game")
                return True
            if self.p0.latest and self.stall_check(self.p0.latest["state"]):
                if not self.stall_recorded:
                    self.stall_recorded = True
                    wf = self.p0.latest["state"].get("waiting_for") or {}
                    self.note(f"STALL: {wf.get('type')} with no actionable "
                              f"submission for 60s+")
                    wire(f"{self.tag}_stall", {"waiting_for": wf})
                    try:
                        await self.export(f"mid_stall_{self.tag}", self.p0)
                        self.say("exported mid_stall")
                    except Exception as e:
                        self.note(f"mid_stall export failed: {e}")
            if time.time() - last_diag > 60 and self.p0.latest:
                last_diag = time.time()
                s = self.p0.latest["state"]
                self.say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                         f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                         f"pp={s.get('priority_player')} P0hand={len(hand_names(s,0))} "
                         f"P1hand={len(hand_names(s,1))} stack={len(s.get('stack') or [])}")
        self.note(f"global timeout ({timeout_s}s) hit")
        return False




def untapped_plains(state, pid):
    out = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                and not o.get("tapped") and obj_name(o) == PLAINS:
            out.append(int(oid))
    return out


def untapped_p0_lands(state):
    # P0 runs no swamps: islands (via shared helper) + plains
    return untapped_lands(state, 0) + untapped_plains(state, 0)


def state_life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


async def game():
    """Alms Collector on P0's board; P1's Divination draws must split 1-1."""
    g = Game("A", DECK_P0, DECK_P1)
    g.obs.update({"alms_cast": False, "div_submitted": False,
                  "alms_turn": None, "div_turn": None})
    await g.start()

    def p0_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, PLAINS))
        plains = sum(1 for n in hn if n == PLAINS)
        return ALMS in hn and lands >= 2 and plains >= 1

    def p1_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return DIV in hn and lands >= 2

    async def p0_tick(st, acts, state):
        if await g.generic_tick(st, acts, state, 0, p0_keep, avoid=(ALMS,)):
            return
        if await g.scan_interactions(st, "P0"):
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 0:
            return
        up = untapped_plains(state, 0)
        ul = untapped_p0_lands(state)
        if (not g.obs["alms_cast"] and not on_bf(state, 0, ALMS)
                and card_in_hand_oid(state, 0, ALMS) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(up) >= 2 and len(ul) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == ALMS:
                    wire("cast_alms", a)
                    await submit_as_is(g.p0, a)
                    g.obs["alms_cast"] = True
                    g.obs["alms_turn"] = state.get("turn_number")
                    g.say(f"P0 casts Alms Collector (turn {g.obs['alms_turn']})")
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(g.p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(g.p0, a)
                return

    async def p1_tick(st, acts, state):
        if await g.generic_tick(st, acts, state, 1, p1_keep, avoid=(DIV,)):
            return
        if await g.scan_interactions(st, "P1"):
            return
        # post: Divination resolved (in gy), stack empty
        if (g.obs["div_submitted"] and not g.post_done
                and in_gy(state, 1, DIV)
                and len(state.get("stack", []) or []) == 0):
            g.say("Divination resolved; exporting POST")
            g.post = await g.export("post", g.p0)
            g.post_done = True
            g.say("exported POST")
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 1:
            return
        ui = untapped_lands(state, 1, ISLAND)
        if (g.obs["alms_cast"] and not g.obs["div_submitted"]
                and on_bf(state, 0, ALMS)
                and card_in_hand_oid(state, 1, DIV) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(ui) >= 3):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == DIV:
                    g.say("P1 main phase: exporting PRE, then casting Divination")
                    g.pre = await g.export("pre", g.p0)
                    g.pre_done = True
                    wire("cast_divination", a)
                    g.obs["div_turn"] = state.get("turn_number")
                    await submit_as_is(g.p1, a)
                    g.obs["div_submitted"] = True
                    g.say(f"P1 casts Divination (turn {g.obs['div_turn']})")
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(g.p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(g.p1, a)
                return

    ok = await g.run_loop(p0_tick, p1_tick,
                          done_pred=lambda: g.obs["div_submitted"])
    return g, ok


def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.78.0/data/card-data.json"))
    low = {k.lower(): k for k in cd}
    out = {}
    for n in ("alms collector", "quantum riddler"):
        c = cd[low[n]]
        out[n] = {"replacements": c["replacements"],
                  "oracle_text": c["oracle_text"]}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def evaluate(g, pe):
    ass = {}
    notes = []
    obs = g.obs

    # ---- A2 parse: the reported gap must still be present in pinned data ----
    alms_reps = (pe.get("alms collector") or {}).get("replacements") or []
    qr_reps = (pe.get("quantum riddler") or {}).get("replacements") or []
    qr_has_draw = any((r.get("event") or "") == "Draw" for r in qr_reps)
    gap = (alms_reps == [])
    ass["A2_parse_gap_present"] = "passed" if gap else "failed"
    notes.append(f"parse alms collector: replacements={json.dumps(alms_reps)[:120]}: "
                 f"{'GAP PRESENT (no Draw replacement)' if gap else 'gap NOT as reported'}")
    notes.append(f"parse quantum riddler (control, 'would draw one or more cards'): "
                 f"{len(qr_reps)} replacement(s), Draw present={qr_has_draw}")

    def snap(state, pid):
        return (len(player_of(state, pid).get("hand", [])),
                lib_count(state, pid),
                len(player_of(state, pid).get("graveyard", [])))

    # ---- A1 setup ----
    if g.pre is not None:
        pre = g.pre
        ok = (on_bf(pre, 0, ALMS)
              and card_in_hand_oid(pre, 1, DIV) is not None
              and len(untapped_lands(pre, 1, ISLAND)) >= 3
              and pre.get("phase") in ("PreCombatMain", "PostCombatMain")
              and state_life(pre, 0) == 20 and state_life(pre, 1) == 20)
        ass["A1_setup"] = "passed" if ok else "failed"
        notes.append(f"pre: alms_on_bf={on_bf(pre, 0, ALMS)}, div_in_hand="
                     f"{card_in_hand_oid(pre, 1, DIV) is not None}, "
                     f"P1_untapped_islands={len(untapped_lands(pre, 1, ISLAND))}, "
                     f"phase={pre.get('phase')}: {'ok' if ok else 'SETUP FAILED'}")
        g.obs["pre_snap"] = {"P0": snap(pre, 0), "P1": snap(pre, 1)}
    else:
        ass["A1_setup"] = "failed"
        notes.append("pre missing: game never reached the Divination cast")

    # ---- A3 draw split + A4 cleanup ----
    if g.pre is not None and g.post is not None:
        post = g.post
        h0pre, l0pre, _ = snap(g.pre, 0)
        h1pre, l1pre, _ = snap(g.pre, 1)
        h0post, l0post, _ = snap(post, 0)
        h1post, l1post, _ = snap(post, 1)
        g.obs["post_snap"] = {"P0": snap(post, 0), "P1": snap(post, 1)}
        say(f"deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        correct = (h0post == h0pre + 1 and l0post == l0pre - 1
                   and h1post == h1pre and l1post == l1pre - 1)
        buggy = (h0post == h0pre and l0post == l0pre
                 and h1post == h1pre + 1 and l1post == l1pre - 2)
        if correct:
            ass["A3_draw_split"] = "passed"
            notes.append(f"A3 split CORRECT per Oracle: P0 {h0pre}->{h0post}/"
                         f"{l0pre}->{l0post}, P1 {h1pre}->{h1post}/"
                         f"{l1pre}->{l1post} (1-1 split)")
        elif buggy:
            ass["A3_draw_split"] = "failed"
            notes.append(f"A3 split BUG: P0 {h0pre}->{h0post} (drew 0), P1 "
                         f"{h1pre}->{h1post} (drew 2) -- no replacement fired")
        else:
            ass["A3_draw_split"] = "failed"
            notes.append(f"A3 split unexpected: P0 {h0pre}->{h0post}/"
                         f"{l0pre}->{l0post}, P1 {h1pre}->{h1post}/"
                         f"{l1pre}->{l1post}")
        if in_gy(post, 1, DIV) and len(post.get("stack", []) or []) == 0:
            ass["A4_cleanup"] = "passed"
            notes.append("A4 cleanup: Divination in P1 gy, stack empty")
        else:
            ass["A4_cleanup"] = "failed"
            notes.append(f"A4 cleanup broken: div_in_gy={in_gy(post, 1, DIV)}, "
                         f"stack={len(post.get('stack', []) or [])}")
    else:
        for k in ("A3_draw_split", "A4_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre/post)")

    # ---- verdict ----
    setup_ok = ass.get("A1_setup") == "passed"
    if setup_ok and ass.get("A3_draw_split") == "failed":
        verdict = "reproduced"
        notes.append("P1's 2-card Divination draw was not replaced 1-1 by "
                     "Alms Collector: the reported outcome reproduces on v0.78.0")
    elif setup_ok and ass.get("A3_draw_split") == "passed":
        verdict = "not-reproduced"
        notes.append("the 1-1 draw split happened in-engine; parse gap is latent")
    else:
        verdict = "blocked"
        notes.append("setup never completed; see notes")
    return ass, notes, verdict


async def main():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    pe = parse_evidence()
    say("alms replacements:", json.dumps(pe["alms collector"]["replacements"]))
    say("riddler replacements:", len(pe["quantum riddler"]["replacements"]))

    g, ok = await game()
    say(f"game finished ok={ok}")

    if g.pre is not None and g.post is None:
        try:
            g.post = await g.export("post", g.p0)
            g.post_done = True
            g.note("post exported at loop end")
        except Exception as e:
            g.note(f"post final export failed: {e}")

    ass, notes, verdict = evaluate(g, pe)
    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_5678.py", "rb").read()).hexdigest(),
        "decks": {"P0": DECK_P0, "P1": DECK_P1},
        "games": {"A": {"ok": ok, "observations": g.obs}},
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Quantum Riddler parsed as a class control in the data only; it was not "
            "driven in-game.",
            "Not tested on the original 2026-07-12 build; verdict is scoped to "
            "v0.78.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0 8x alms collector + 26 plains + 26 island vs P1 8x "
                      "divination + 52 island (P1 casts Divination on their main "
                      "phase with Alms Collector on P0's board).",
        "contract_line": "Per Oracle/CR 121.2a, P1's 2-card draw instruction is "
                         "replaced: P0 draws 1 and P1 draws 1.",
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


asyncio.run(main())
