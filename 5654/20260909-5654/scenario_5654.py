#!/usr/bin/env python3
"""Issue #5654: Notion Thief + Plagiarize -- compound "skips that draw and
you draw a card" substitute drops to Unimplemented (one fix, two cards).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Issue (internal triage, 2026-07-12): both cards' replacement substitute is the
identical compound "instead that player skips that draw and you draw a card".
In the pinned v0.78.0 card-data.json the skip half parses to
execute.effect.type == "Unimplemented" while the "you draw a card" half parses
to Draw{Fixed 1, Controller}. Hullbreacher's identical-antecedent single-action
substitute parses fully to Token. The classifier verdict is
unsupported_aspect. The player-facing question: does the replacement do
anything in a real game (skip + controller draw per Oracle), or is it inert
/ half-applied because of the Unimplemented node?

Oracle text (verified from pinned v0.78.0 card-data.json):
  Notion Thief {2}{U}{B} 3/2 Flash creature:
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead that player skips that draw and you draw
     a card."
  Plagiarize {3}{U} instant:
    "Until end of turn, if target player would draw a card, instead that
     player skips that draw and you draw a card."
  Hullbreacher (control):
    "If an opponent would draw a card except the first one they draw in each
     of their draw steps, instead you create a Treasure token." -> Token.

Game A (Notion Thief): P0 casts Notion Thief, then P1 casts Divination
("Draw two cards.") during their own main phase. Both draws are non-first
draw-step draws, so per Oracle both are replaced: P1 skips (hand/lib
unchanged), P0 draws 2.

Game B (Plagiarize): P0 casts Plagiarize targeting P1 during P1's Upkeep
(instant timing; must precede P1's draw-step draw since Plagiarize has no
first-draw exemption). P1's draw-step draw is then replaced: P1 hand/lib
unchanged, P0 draws 1.

Assertions (per game):
  A1_setup      pre: key permanent/spell in place, mana available, life 20/20.
  A2_parse      pinned card-data still shows the Unimplemented skip node with
                the Draw{1,Controller} sub-ability (the reported class gap);
                Hullbreacher control parses to Token (no Unimplemented).
  A3_target     (B only) Plagiarize target prompt offered P1 and accepted.
  A4_skip_holds draw events skipped: victim hand/library unchanged by them.
  A5_ctrl_draws controller drew the replaced draws: P0 hand/library deltas.
  A6_cleanup    spell in graveyard, stack empty, game proceeding.

Verdict rule: reproduced iff setup completed for a game and the in-engine
draw replacement deviates from Oracle (A4 or A5 failed) -- e.g. inert (victim
draws normally) or half-applied (victim draws AND controller draws).
not-reproduced iff all outcome assertions pass for every game whose setup
completed (parse gap documented as latent). blocked iff no setup completed.

Evidence: evidence/5654/<run-id>/pre_A.json, post_A.json, pre_B.json,
post_B.json, parse_evidence.json, run.json, manifest.sha256, summary.png,
scenario_5654.py, wire_log.jsonl, scenario_run.log
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
RUN_ID = "20260909-5654"
ISSUE = 5654
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

THIEF = "notion thief"
PLAG = "plagiarize"
DIV = "divination"
ISLAND = "island"
SWAMP = "swamp"

A_DECK_P0 = [(THIEF, 8), (ISLAND, 26), (SWAMP, 26)]
A_DECK_P1 = [(DIV, 8), (ISLAND, 52)]
B_DECK_P0 = [(PLAG, 8), (ISLAND, 52)]
B_DECK_P1 = [(ISLAND, 60)]

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
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-5654",
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


async def game_a():
    """Notion Thief: P1's Divination draws must be skipped; P0 draws 2."""
    g = Game("A", A_DECK_P0, A_DECK_P1)
    g.obs.update({"thief_cast": False, "div_submitted": False,
                  "thief_turn": None, "div_turn": None,
                  "target_policy": None})
    await g.start()

    def p0_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n in (ISLAND, SWAMP))
        return THIEF in hn and lands >= 2

    def p1_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return DIV in hn and lands >= 2

    async def p0_tick(st, acts, state):
        if await g.generic_tick(st, acts, state, 0, p0_keep, avoid=(THIEF,)):
            return
        if await g.scan_interactions(st, "P0"):
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 0:
            return
        ul = untapped_lands(state, 0)
        ui = untapped_lands(state, 0, ISLAND)
        us = untapped_lands(state, 0, SWAMP)
        if (not g.obs["thief_cast"] and not on_bf(state, 0, THIEF)
                and card_in_hand_oid(state, 0, THIEF) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(ui) >= 1 and len(us) >= 1 and len(ul) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == THIEF:
                    wire("A_cast_thief", a)
                    await submit_as_is(g.p0, a)
                    g.obs["thief_cast"] = True
                    g.obs["thief_turn"] = state.get("turn_number")
                    g.say(f"P0 casts Notion Thief (turn {g.obs['thief_turn']})")
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
            g.say("Divination resolved; exporting POST_A")
            g.post = await g.export("post_A", g.p0)
            g.post_done = True
            g.say("exported POST_A")
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 1:
            return
        ui = untapped_lands(state, 1, ISLAND)
        if (g.obs["thief_cast"] and not g.obs["div_submitted"]
                and on_bf(state, 0, THIEF)
                and card_in_hand_oid(state, 1, DIV) is not None
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(ui) >= 3):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == DIV:
                    g.say("P1 main phase: exporting PRE_A, then casting Divination")
                    g.pre = await g.export("pre_A", g.p0)
                    g.pre_done = True
                    wire("A_cast_divination", a)
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


async def game_b():
    """Plagiarize: cast targeting P1 during P1's Upkeep; P1's draw-step draw
    must be skipped and P0 must draw 1."""
    g = Game("B", B_DECK_P0, B_DECK_P1)
    g.obs.update({"plag_cast": False, "plag_turn": None, "target_ok": False,
                  "target_prompt_seen": False})
    await g.start()

    def p0_keep(state, pid):
        hn = hand_names(state, pid)
        lands = sum(1 for n in hn if n == ISLAND)
        return PLAG in hn and lands >= 2

    def p1_keep(state, pid):
        return True  # dummy keeps

    def plag_target_policy(opp, rtype, stype, chs):
        # answer the Plagiarize "target player" schema sequence with seat 1
        if rtype != "schema" or stype != "sequence":
            return None
        if not g.obs["plag_cast"] or g.obs["target_ok"]:
            return None
        for ch in chs:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and d.get("seat") == 1:
                    return {"interactionId": opp.get("interactionId"),
                            "response": {"type": "sequence",
                                         "data": {"choiceIds": [ch["id"]]}}}
        return None

    async def p0_tick(st, acts, state):
        if await g.generic_tick(st, acts, state, 0, p0_keep, avoid=(PLAG,)):
            return
        if await g.scan_interactions(st, "P0", extra=plag_target_policy):
            if any(s.get("spec") == "sequence"
                   for s in g.obs["interaction_shapes"]):
                g.obs["target_prompt_seen"] = True
            return
        # post: P1 has passed their draw step (main phase), Plagiarize in gy
        if (g.obs["plag_cast"] and not g.post_done
                and in_gy(state, 0, PLAG)
                and state.get("active_player") == 1
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and len(state.get("stack", []) or []) == 0):
            g.say("P1 past draw step; exporting POST_B")
            g.post = await g.export("post_B", g.p0)
            g.post_done = True
            g.say("exported POST_B")
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 0:
            return
        ui = untapped_lands(state, 0, ISLAND)
        # cast during P1's Upkeep (must precede P1's draw-step draw)
        if (not g.obs["plag_cast"]
                and card_in_hand_oid(state, 0, PLAG) is not None
                and state.get("active_player") == 1
                and state.get("phase") == "Upkeep"
                and len(ui) >= 4):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == PLAG:
                    g.say("P1 Upkeep: exporting PRE_B, then casting Plagiarize @P1")
                    g.pre = await g.export("pre_B", g.p0)
                    g.pre_done = True
                    wire("B_cast_plagiarize", a)
                    g.obs["plag_turn"] = state.get("turn_number")
                    await submit_as_is(g.p0, a)
                    g.obs["plag_cast"] = True
                    g.say(f"P0 casts Plagiarize targeting P1 (turn {g.obs['plag_turn']})")
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
        if await g.generic_tick(st, acts, state, 1, p1_keep):
            return
        if await g.scan_interactions(st, "P1"):
            return
        if (state.get("waiting_for") or {}).get("type") != "Priority" \
                or state.get("priority_player") != 1:
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
                          done_pred=lambda: g.obs["plag_cast"])
    return g, ok


def parse_evidence():
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.78.0/data/card-data.json"))
    low = {k.lower(): k for k in cd}
    out = {}
    for n in ("notion thief", "plagiarize", "hullbreacher"):
        out[n] = {"replacements": cd[low[n]]["replacements"],
                  "oracle_text": cd[low[n]]["oracle_text"]}
    with open(f"{EVDIR}/parse_evidence.json", "w") as f:
        json.dump(out, f, indent=1)
    return out


def summarize_parse(pe):
    """Reduce the parse to the fields the issue is about."""
    res = {}
    for n, e in pe.items():
        reps = e["replacements"] or []
        r0 = reps[0] if reps else {}
        ex = (r0.get("execute") or {})
        eff = ex.get("effect") or {}
        sub = ex.get("sub_ability") or {}
        seff = sub.get("effect") or {}
        res[n] = {
            "event": r0.get("event"),
            "condition": (r0.get("condition") or {}).get("type"),
            "execute_effect": eff.get("type"),
            "execute_desc": eff.get("description"),
            "sub_effect": seff.get("type"),
            "sub_count": (seff.get("count") or {}).get("value"),
            "sub_target": (seff.get("target") or {}).get("type"),
        }
    return res


def evaluate(gA, gB, parse_sum):
    ass = {}
    notes = []
    obsA, obsB = gA.obs, gB.obs

    # ---- A2 parse checks (both cards + hullbreacher control) ----
    nt = parse_sum["notion thief"]
    pl = parse_sum["plagiarize"]
    hb = parse_sum["hullbreacher"]
    gap_nt = (nt["execute_effect"] == "Unimplemented"
              and nt["sub_effect"] == "Draw" and nt["sub_count"] == 1
              and nt["sub_target"] == "Controller")
    gap_pl = (pl["execute_effect"] == "Unimplemented"
              and pl["sub_effect"] == "Draw" and pl["sub_count"] == 1
              and pl["sub_target"] == "Controller")
    hb_ok = (hb["execute_effect"] == "Token" and hb["sub_effect"] is None)
    ass["A2A_parse_notion"] = "passed" if gap_nt else "failed"
    notes.append(f"parse notion thief: execute={nt['execute_effect']} "
                 f"({nt['execute_desc']}), sub={nt['sub_effect']}x{nt['sub_count']} "
                 f"->{nt['sub_target']}: {'GAP PRESENT' if gap_nt else 'gap not as reported'}")
    ass["A2B_parse_plagiarize"] = "passed" if gap_pl else "failed"
    notes.append(f"parse plagiarize: execute={pl['execute_effect']} "
                 f"({pl['execute_desc']}), sub={pl['sub_effect']}x{pl['sub_count']} "
                 f"->{pl['sub_target']}: {'GAP PRESENT' if gap_pl else 'gap not as reported'}")
    notes.append(f"parse hullbreacher control: execute={hb['execute_effect']} "
                 f"(single-action substitute fully parsed: "
                 f"{'yes' if hb_ok else 'NO'})")

    def snap(state, pid):
        return (len(player_of(state, pid).get("hand", [])),
                lib_count(state, pid),
                len(player_of(state, pid).get("graveyard", [])))

    # ---- Game A ----
    if gA.pre is not None:
        pre = gA.pre
        ok = (on_bf(pre, 0, THIEF)
              and card_in_hand_oid(pre, 1, DIV) is not None
              and len(untapped_lands(pre, 1, ISLAND)) >= 3
              and pre.get("phase") in ("PreCombatMain", "PostCombatMain")
              and state_life(pre, 0) == 20 and state_life(pre, 1) == 20)
        ass["A1A_setup"] = "passed" if ok else "failed"
        notes.append(f"pre_A: thief_on_bf={on_bf(pre,0,THIEF)}, div_in_hand="
                     f"{card_in_hand_oid(pre,1,DIV) is not None}, "
                     f"P1_untapped_islands={len(untapped_lands(pre,1,ISLAND))}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
        gA.obs["preA_snap"] = {"P0": snap(pre, 0), "P1": snap(pre, 1)}
    else:
        ass["A1A_setup"] = "failed"
        notes.append("pre_A missing: Notion Thief game never reached the Divination cast")
    if gA.pre is not None and gA.post is not None:
        post = gA.post
        h0pre, l0pre, _ = snap(gA.pre, 0)
        h1pre, l1pre, _ = snap(gA.pre, 1)
        h0post, l0post, _ = snap(post, 0)
        h1post, l1post, _ = snap(post, 1)
        gA.obs["postA_snap"] = {"P0": snap(post, 0), "P1": snap(post, 1)}
        say(f"[A] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        # A4: victim skips both draws (Divination itself left the hand: -1)
        if h1post == h1pre - 1 and l1post == l1pre:
            ass["A4A_skip_holds"] = "passed"
            notes.append(f"A4A skip holds: P1 hand {h1pre}->{h1post} "
                         f"(-1 = cast Divination, +0 draws), "
                         f"library {l1pre}->{l1post} (both draws skipped)")
        else:
            ass["A4A_skip_holds"] = "failed"
            notes.append(f"A4A skip BROKEN: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (expected unchanged)")
        # A5: controller draws both
        if h0post == h0pre + 2 and l0post == l0pre - 2:
            ass["A5A_ctrl_draws"] = "passed"
            notes.append(f"A5A controller draws: P0 hand {h0pre}->{h0post}, "
                         f"library {l0pre}->{l0post}")
        else:
            ass["A5A_ctrl_draws"] = "failed"
            notes.append(f"A5A controller draw BROKEN: P0 hand {h0pre}->{h0post} "
                         f"(expected +2), library {l0pre}->{l0post} (expected -2)")
        if in_gy(post, 1, DIV) and len(post.get("stack", []) or []) == 0:
            ass["A6A_cleanup"] = "passed"
            notes.append("A6A cleanup: Divination in P1 gy, stack empty, game proceeding")
        else:
            ass["A6A_cleanup"] = "failed"
            notes.append(f"A6A cleanup broken: div_in_gy={in_gy(post,1,DIV)}, "
                         f"stack={len(post.get('stack',[]) or [])}")
    else:
        for k in ("A4A_skip_holds", "A5A_ctrl_draws", "A6A_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre/post)")

    # ---- Game B ----
    if gB.pre is not None:
        pre = gB.pre
        ok = (card_in_hand_oid(pre, 0, PLAG) is not None
              and len(untapped_lands(pre, 0, ISLAND)) >= 4
              and pre.get("active_player") == 1
              and pre.get("phase") == "Upkeep"
              and state_life(pre, 0) == 20 and state_life(pre, 1) == 20)
        ass["A1B_setup"] = "passed" if ok else "failed"
        notes.append(f"pre_B: plag_in_hand={card_in_hand_oid(pre,0,PLAG) is not None}, "
                     f"P0_untapped_islands={len(untapped_lands(pre,0,ISLAND))}, "
                     f"active={pre.get('active_player')} phase={pre.get('phase')}: "
                     f"{'ok' if ok else 'SETUP FAILED'}")
        gB.obs["preB_snap"] = {"P0": snap(pre, 0), "P1": snap(pre, 1)}
    else:
        ass["A1B_setup"] = "failed"
        notes.append("pre_B missing: Plagiarize game never reached the Upkeep cast")
    if obsB["target_prompt_seen"]:
        ass["A3B_target_offered"] = "passed" if obsB["plag_cast"] else "failed"
        notes.append(f"A3B target: Plagiarize target prompt seen, cast "
                     f"{'completed' if obsB['plag_cast'] else 'DID NOT complete'}")
    else:
        ass["A3B_target_offered"] = "failed" if obsB["plag_cast"] else "not-run"
        notes.append("A3B target: no target prompt observed in wire log")
    if gB.pre is not None and gB.post is not None:
        post = gB.post
        h0pre, l0pre, _ = snap(gB.pre, 0)
        h1pre, l1pre, _ = snap(gB.pre, 1)
        h0post, l0post, _ = snap(post, 0)
        h1post, l1post, _ = snap(post, 1)
        gB.obs["postB_snap"] = {"P0": snap(post, 0), "P1": snap(post, 1)}
        say(f"[B] deltas P0 hand {h0pre}->{h0post} lib {l0pre}->{l0post}; "
            f"P1 hand {h1pre}->{h1post} lib {l1pre}->{l1post}")
        # pre_B hand includes Plagiarize; cast consumes 1, replaced draw adds 1
        if h1post == h1pre and l1post == l1pre:
            ass["A4B_skip_holds"] = "passed"
            notes.append(f"A4B skip holds: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1pre} (draw-step draw skipped)")
        else:
            ass["A4B_skip_holds"] = "failed"
            notes.append(f"A4B skip BROKEN: P1 hand {h1pre}->{h1post}, "
                         f"library {l1pre}->{l1post} (expected unchanged)")
        if h0post == h0pre and l0post == l0pre - 1:
            ass["A5B_ctrl_draws"] = "passed"
            notes.append(f"A5B controller draws: P0 hand {h0pre}->{h0post} "
                         f"(cast -1, replaced draw +1), library {l0pre}->{l0post}")
        else:
            ass["A5B_ctrl_draws"] = "failed"
            notes.append(f"A5B controller draw BROKEN: P0 hand {h0pre}->{h0post} "
                         f"(expected {h0pre}), library {l0pre}->{l0post} (expected -1)")
        if in_gy(post, 0, PLAG) and len(post.get("stack", []) or []) == 0:
            ass["A6B_cleanup"] = "passed"
            notes.append("A6B cleanup: Plagiarize in P0 gy, stack empty, game proceeding")
        else:
            ass["A6B_cleanup"] = "failed"
            notes.append(f"A6B cleanup broken: plag_in_gy={in_gy(post,0,PLAG)}, "
                         f"stack={len(post.get('stack',[]) or [])}")
    else:
        for k in ("A4B_skip_holds", "A5B_ctrl_draws", "A6B_cleanup"):
            ass[k] = "not-run"
            notes.append(f"{k} not-run (missing pre/post)")

    # ---- verdict ----
    a_ok = ass.get("A1A_setup") == "passed"
    b_ok = ass.get("A1B_setup") == "passed"
    a_bad = a_ok and (ass.get("A4A_skip_holds") == "failed"
                      or ass.get("A5A_ctrl_draws") == "failed")
    b_bad = b_ok and (ass.get("A4B_skip_holds") == "failed"
                      or ass.get("A5B_ctrl_draws") == "failed")
    a_good = a_ok and all(ass.get(k) == "passed" for k in
                          ("A4A_skip_holds", "A5A_ctrl_draws", "A6A_cleanup"))
    b_good = b_ok and all(ass.get(k) == "passed" for k in
                          ("A4B_skip_holds", "A5B_ctrl_draws", "A6B_cleanup"))
    tested = [x for x, okv in (("A", a_ok), ("B", b_ok)) if okv]
    if a_bad or b_bad:
        verdict = "reproduced"
        notes.append("draw replacement deviates from Oracle in-game "
                     f"(A_bad={a_bad}, B_bad={b_bad})")
    elif tested and all((a_good if t == "A" else b_good) for t in tested):
        verdict = "not-reproduced"
        notes.append("in-engine draw replacement matches Oracle for every "
                     "completed game; the Unimplemented parse node is latent")
    elif not tested:
        verdict = "blocked"
        notes.append("no game reached its cast; see notes")
    else:
        verdict = "reproduced"
        notes.append("mixed outcome assertions on the reported contract")
    return ass, notes, verdict


def state_life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


async def main():
    t_start = time.time()
    say(f"starting issue #{ISSUE} run {RUN_ID}")
    pe = parse_evidence()
    parse_sum = summarize_parse(pe)
    say("parse summary:", json.dumps(parse_sum))

    gA, okA = await game_a()
    say(f"game A finished ok={okA}")
    gB, okB = await game_b()
    say(f"game B finished ok={okB}")

    # final post exports if the loop ended without them
    for g, tag in ((gA, "A"), (gB, "B")):
        if g.pre is not None and g.post is None:
            try:
                g.post = await g.export(f"post_{tag}", g.p0)
                g.post_done = True
                g.note(f"post_{tag} exported at loop end")
            except Exception as e:
                g.note(f"post_{tag} final export failed: {e}")

    ass, notes, verdict = evaluate(gA, gB, parse_sum)
    dur = time.time() - t_start
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "parse_summary": parse_sum,
        "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_5654.py", "rb").read()).hexdigest(),
        "decks": {
            "A_P0": A_DECK_P0, "A_P1": A_DECK_P1,
            "B_P0": B_DECK_P0, "B_P1": B_DECK_P1,
        },
        "games": {
            "A": {"ok": okA, "observations": gA.obs},
            "B": {"ok": okB, "observations": gB.obs},
        },
        "assertions": ass,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x key-card deck density is a test-harness convenience (engine accepts "
            ">4-of for custom games); exercised behavior is the shipped card text.",
            "Magus of the Chains shares the same Oracle text and parse class but "
            "was not driven (same-class coverage via Plagiarize/Notion Thief).",
            "Not tested on the original 2026-07-12 build; verdict is scoped to "
            "v0.78.0, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "A: P0 8x notion thief + 26 island + 26 swamp vs P1 8x "
                      "divination + 52 island (P1 casts Divination with Thief on "
                      "board). B: P0 8x plagiarize + 52 island vs P1 60 island "
                      "(P0 casts Plagiarize @P1 during P1's Upkeep).",
        "contract_line": "Per Oracle, each affected draw is skipped by the "
                         "victim and drawn by the controller instead.",
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
