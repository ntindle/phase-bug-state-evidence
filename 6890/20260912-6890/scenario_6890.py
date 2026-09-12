#!/usr/bin/env python3
"""Issue #6890: Mana colors on multi-color sources aren't displaying for selection.

Reported (Discord 2026-08-02, status:confirmed, area:frontend): when selecting
among multicolor mana abilities, the choice UI does not show which mana color
each option will produce ("Can process of elimination it, but, y'know").

Triage acceptance criteria:
  - Every mana-choice option displays the color and amount it will produce.
  - Options remain distinguishable for permanents with multiple mana abilities.
  - The submitted action still maps to the engine-provided ability without
    frontend rules inference.

Investigation pinned the live defect to the client's ManaSourceSelectionUI
(client/src/components/mana/ManaPaymentUI.tsx): when the engine reaches
WaitingFor::ManaSourceSelection (sacrificial mana abilities during payment --
e.g. Lotus Petal's "{T}, Sacrifice: Add one mana of any color"), the modal
renders one row per engine-supplied option showing ONLY the source permanent's
name plus the "sacrifice" caption. Each option carries mana_type/output color
data, but the UI never renders it -- so N options from one multi-color source
are visually indistinguishable.

Behavioral contract (native engine v0.80.0 / protocol 69, two human seats):
  P0 casts two Lotus Petals (free), then casts Aether Vial ({1}) with
  payment_mode AutoExceptSacrificialMana (client-selectable wire enum; the
  shipped client's spell-payment preference stamps the same mode). With no
  non-sacrificial sources available the engine must offer
  WaitingFor::ManaSourceSelection with one option per petal.
  A1 setup_ok       2 petals on P0 BF untapped, vial in P0 hand
  A2 selection_prompted  waiting_for.type == "ManaSourceSelection" after cast
  A3 options_carry_color  >=2 options, each with source/mana_type/output
                    (the data the UI needs to distinguish them)
  A4 ui_drops_color the shipped client's ManaSourceSelectionUI renders each
                    option as source-name + "sacrifice" only (code evidence:
                    no color rendered) -> the reported outcome
  A5 selection_works submitting ActivateManaSource with an option, then
                    answering the deferred ChooseManaColor, sacrifices a
                    petal, floats that color, vial resolves to BF
  A6 cleanup        no dangling selection prompt; game proceeds

Verdict: reproduced iff A1+A2+A3 pass and A4 holds (A5 passing shows the flow
completes; the defect is presentational).
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6890"
EVDIR = f"{BACKFILL}/evidence/6890/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

PETAL = "Lotus Petal"
VIAL = "Aether Vial"
FOREST = "Forest"
P0_DECK = [(PETAL, 12), (VIAL, 12), (FOREST, 36)]
P1_DECK = [(FOREST, 60)]
TIMEOUT = 900

ST = {}
WF_SEEN = []
SUBMITTED_IIDS = set()


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",       # SETUP -> SELECT -> RESOLVE -> DONE
        "stop": False,
        "turn_cap": 20,
        "server_hello": None,
        "petal_cast": False,
        "petals_wanted": 2,
        "vial_cast": False,
        "pre_exported": False,
        "post_exported": False,
        "mss_seen": False,
        "mss_options": None,
        "mss_option_colors": [],
        "mss_answered": False,
        "mss_chosen_color": None,
        "cmc_seen": False,
        "cmc_options": [],
        "cmc_answered": False,
        "cmc_chosen": None,
        "vial_resolved": False,
        "rejections": [],
    })
    WF_SEEN.clear()
    SUBMITTED_IIDS.clear()


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
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


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


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


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


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf
               or WF_SEEN[-1][2] != ST["stage"]):
        WF_SEEN.append((wf, wf_player(state), ST["stage"]))
        wire("waiting_for", {"type": wf, "stage": ST["stage"],
                             "data_keys": sorted(wf_data(state).keys())})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST['stage']}")


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
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
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


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def observe_mss(state):
    """Capture the ManaSourceSelection prompt once."""
    if ST["mss_seen"] or wf_type(state) != "ManaSourceSelection":
        return
    if wf_player(state) != 0:
        return
    ST["mss_seen"] = True
    ST["stage"] = "SELECT"
    opts = wf_data(state).get("options") or []
    ST["mss_options"] = opts
    colors = []
    for o in opts:
        colors.append({
            "mana_type": o.get("mana_type"),
            "output": o.get("output"),
            "atomic_combination": o.get("atomic_combination"),
            "source_object_id": ((o.get("source") or {}).get("object_id")),
            "ability_index": o.get("ability_index"),
            "penalty": o.get("penalty"),
        })
    ST["mss_option_colors"] = colors
    wire("mss_prompt", {"n_options": len(opts), "options": colors,
                        "stage": ST["stage"]})
    say(f"ManaSourceSelection: {len(opts)} options")
    for i, oc in enumerate(colors):
        say(f"  option {i}: mana_type={oc['mana_type']} "
            f"output={oc['output']} source={oc['source_object_id']}")
    # also capture the viewer_interaction projection of this prompt
    vi = (C0.latest or {}).get("viewer_interaction")
    if vi:
        wire("mss_viewer_interaction", vi)
        with open(f"{EVDIR}/mss_viewer_interaction.json", "w") as f:
            json.dump(vi, f, indent=1)


async def answer_mss(c, state):
    """Submit ActivateManaSource with the first option's exact selection."""
    if ST["mss_answered"]:
        return False
    if wf_type(state) != "ManaSourceSelection" or wf_player(state) != 0:
        return False
    opts = wf_data(state).get("options") or []
    if not opts:
        return False
    sel = copy.deepcopy(opts[0])
    ST["mss_chosen_color"] = sel.get("mana_type")
    wire("mss_answer", {"chosen": sel, "stage": ST["stage"]})
    say(f"[P0] answers ManaSourceSelection with mana_type="
        f"{ST['mss_chosen_color']}")
    await submit_as_is(c, {"type": "ActivateManaSource",
                           "data": {"selection": sel}})
    ST["mss_answered"] = True
    ST["stage"] = "RESOLVE"
    return True


async def answer_cmc(c, state):
    """Answer the deferred color choice with the first offered color."""
    if ST["cmc_answered"]:
        return False
    if wf_type(state) != "ChooseManaColor" or wf_player(state) != 0:
        return False
    data = wf_data(state)
    choice = data.get("choice") or {}
    cdata = choice.get("data") or {}
    options = cdata.get("options") or []
    if not options:
        wire("cmc_no_options", {"data": data, "stage": ST["stage"]})
        say("[P0] ChooseManaColor has no options; holding")
        return True
    pick = options[0]
    ST["cmc_seen"] = True
    ST["cmc_options"] = options
    wire("cmc_prompt", {"options": options, "pick": pick,
                        "stage": ST["stage"]})
    say(f"[P0] ChooseManaColor options={options}; picking {pick}")
    await submit_as_is(c, {"type": "ChooseManaColor",
                           "data": {"choice": {"type": "SingleColor",
                                               "data": pick},
                                    "count": 1}})
    ST["cmc_answered"] = True
    ST["cmc_chosen"] = pick
    return True


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    # never pass priority past the selection prompt
    if await answer_mss(c, state):
        return True
    if await answer_cmc(c, state):
        return True
    if not is_my_main(state, pid):
        return False
    # SETUP: cast petals (free) until we have two
    if ST["stage"] == "SETUP" \
            and len(bf_named(state, pid, PETAL)) < ST["petals_wanted"]:
        oid = find_hand(state, pid, PETAL)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            ST["petal_cast"] = True
            say(f"[P0] casts Lotus Petal "
                f"({len(bf_named(state, pid, PETAL)) + 1}"
                f"/{ST['petals_wanted']})")
            return True
    # SELECT: cast the vial with sacrificial-mana mode
    if ST["stage"] == "SETUP" and ST["petal_cast"] \
            and len(bf_named(state, pid, PETAL)) >= ST["petals_wanted"] \
            and not ST["vial_cast"]:
        oid = find_hand(state, pid, VIAL)
        a = castspell_advertised(acts, oid)
        if a:
            if not ST["pre_exported"]:
                await export_now("pre.json")
                ST["pre_exported"] = True
            sub = copy.deepcopy(a)
            # wire enum is internally tagged: {"type": "AutoExceptSacrificialMana"}
            sub["data"]["payment_mode"] = {"type": "AutoExceptSacrificialMana"}
            wire("vial_cast_submission", {"action": sub,
                                         "stage": ST["stage"]})
            await submit_as_is(c, sub)
            ST["vial_cast"] = True
            say("[P0] casts Aether Vial (AutoExceptSacrificialMana)")
            return True
    return False


async def p1_tick(c, pid, state, acts):
    if await answer_mss(c, state):
        return True
    if await answer_cmc(c, state):
        return True
    if not is_my_main(state, pid):
        return False
    lid = find_hand(state, pid, FOREST)
    if lid:
        a = next((x for x in acts if x["type"] == "PlayLand"
                  and str(x.get("data", {}).get("object_id")) == lid), None)
        if a:
            await submit_as_is(c, a)
            return True
    return False


async def empty_declare(c, acts):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def empty_blockers(c, acts):
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    record_wf(state)
    rejs = drain_rejections(c)
    # a rejected vial cast (e.g. malformed payment_mode) must be retried,
    # not silently dropped
    if ST["vial_cast"] and not ST["mss_seen"] \
            and any(r["type"] == "Error" for r in rejs):
        ST["vial_cast"] = False
        say(f"[{c.name}] vial cast errored; resetting to retry")
    observe_mss(state)
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            return True
    # vial resolution watch
    if ST["stage"] == "RESOLVE" and not ST["vial_resolved"]:
        if bf_named(state, pid if False else 0, VIAL):
            ST["vial_resolved"] = True
            say("Aether Vial resolved onto P0 battlefield")
            wire("vial_resolved", {"turn": state.get("turn_number"),
                                   "phase": state.get("phase")})
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if await empty_declare(c, acts):
            return True
    if (state.get("phase") or "") == "DeclareBlockers":
        if await empty_blockers(c, acts):
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # never pass priority while a selection/color prompt names us
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt in ("ManaSourceSelection", "ChooseManaColor"):
        wire("named_prompt_held", {"wf": wt, "who": c.name,
                                   "stage": ST["stage"]})
        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def main():
    reset()
    global C0, C1
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])
    # parse evidence: Lotus Petal's mana ability from the pinned dataset
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.80.0/data/card-data.json"))
        lp = cd["lotus petal"]
        with open(f"{EVDIR}/parse_evidence.json", "w") as f:
            json.dump({"card": lp["name"],
                       "oracle_text": lp["oracle_text"],
                       "abilities": lp["abilities"],
                       "mana_cost": lp["mana_cost"]}, f, indent=1)
        say("parse_evidence.json written")
    except Exception as e:
        say(f"parse evidence FAILED: {e}")
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        if st0:
            state = st0.get("state")
            if ST["vial_resolved"] and not ST["post_exported"] \
                    and not (state.get("stack") or []) \
                    and wf_type(state) not in ("ManaSourceSelection",):
                await export_now("post.json")
                ST["post_exported"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("post.json exported; stopping")
        if acted0 or acted1:
            last_progress = time.time()
        if st0 and (st0.get("state", {}).get("turn_number") or 0) > \
                ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if time.time() - last_progress > 180:
            say("no progress for 180s; dumping state and stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": st.get("state", {}).get("waiting_for"),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST["stage"]})
            break
        await asyncio.sleep(0.15)

    # ---- assertions ----
    A = {}
    D = {}

    def load_env(fn):
        try:
            return json.loads(open(f"{EVDIR}/{fn}").read())
        except Exception as e:
            D[f"{fn}_err"] = str(e)[:120]
            return None

    def env_state(env):
        if not env:
            return None
        s = env.get("state")
        return s if isinstance(s, dict) else json.loads(s)

    pre = load_env("pre.json")
    post = load_env("post.json")
    pre_s, post_s = env_state(pre), env_state(post)

    # A1: setup (two petals for >=2 selection options)
    petals_pre = bf_named(pre_s, 0, PETAL) if pre_s else []
    petal_bf_pre = len(petals_pre) >= 2
    petal_untapped = sum(1 for oid in petals_pre
                         if not pre_s["objects"][oid].get("tapped")) >= 2 \
        if pre_s else False
    vial_hand_pre = find_hand(pre_s, 0, VIAL) is not None if pre_s else False
    A["A1_setup_ok"] = "passed" if (petal_bf_pre and petal_untapped
                                    and vial_hand_pre) else "failed"
    D["A1_setup_ok_detail"] = (f"petal_bf={petal_bf_pre} "
                               f"petal_untapped={petal_untapped} "
                               f"vial_in_hand={vial_hand_pre}")

    # A2: selection prompted
    A["A2_selection_prompted"] = "passed" if ST["mss_seen"] else "failed"
    D["A2_selection_prompted_detail"] = (
        f"mss_seen={ST['mss_seen']} waiting_for_seq="
        f"{[w[0] for w in WF_SEEN]}")

    # A3: options carry color data; >=2 options from the multi-color sources
    # (each option = one sacrificial source; the UI must tell them apart)
    opts = ST["mss_options"] or []
    color_set = {o.get("mana_type") for o in (ST["mss_option_colors"] or [])}
    distinct_sources = {o.get("source_object_id")
                        for o in (ST["mss_option_colors"] or [])}
    have_fields = all(o.get("mana_type") and o.get("output")
                      is not None and o.get("source_object_id") is not None
                      for o in (ST["mss_option_colors"] or []))
    A["A3_options_carry_color"] = "passed" if (
        ST["mss_seen"] and len(opts) >= 2 and have_fields
        and len(distinct_sources) == len(opts)) else (
        "failed" if ST["mss_seen"] else "not-run")
    D["A3_options_carry_color_detail"] = (
        f"n_options={len(opts)} distinct_sources={len(distinct_sources)} "
        f"mana_types={sorted(str(c) for c in color_set)} "
        f"outputs={[o.get('output') for o in (ST['mss_option_colors'] or [])]}")

    # A4: the shipped client UI drops the color (code evidence)
    ui_path = ("/home/hatch/workspace/dev/phase/client/src/components/mana/"
               "ManaPaymentUI.tsx")
    try:
        src = open(ui_path).read()
        start = src.index("export function ManaSourceSelectionUI()")
        end = src.index("\n}\n", start) + 3
        body = src[start:end]
        renders_name = "source?.name" in body
        renders_sac = "manaSourceSelection.sacrifice" in body
        renders_color = ("mana_type" in body or "ManaSymbol" in body
                         or "output" in body)
        ui_drops = renders_name and renders_sac and not renders_color
    except Exception as e:
        ui_drops, body = False, f"read failed: {e}"
    with open(f"{EVDIR}/ui_code_evidence.txt", "w") as f:
        f.write(f"client checkout: ~/workspace/dev/phase "
                f"(see run.json client_source_commit)\n")
        f.write(f"file: client/src/components/mana/ManaPaymentUI.tsx\n\n")
        f.write(body if isinstance(body, str) else "")
    A["A4_ui_drops_color"] = "passed" if ui_drops else "failed"
    D["A4_ui_drops_color_detail"] = (
        f"renders source name={renders_name}, renders 'sacrifice' caption="
        f"{renders_sac}, renders any color={renders_color} -> "
        f"options visually indistinguishable: {ui_drops}")

    # A5: selection works end-to-end
    petal_gy = False
    vial_bf = False
    if post_s:
        petal_gy = any(oname(o) == PETAL and o.get("zone") == "Graveyard"
                       and o.get("owner") == 0
                       for o in post_s["objects"].values())
        vial_bf = len(bf_named(post_s, 0, VIAL)) > 0
    A["A5_selection_works"] = "passed" if (
        ST["mss_answered"] and ST["cmc_answered"] and petal_gy and vial_bf) else (
        "failed" if ST["mss_answered"] else "not-run")
    D["A5_selection_works_detail"] = (
        f"answered={ST['mss_answered']} chosen_color={ST['mss_chosen_color']} "
        f"cmc_answered={ST['cmc_answered']} cmc_chosen={ST['cmc_chosen']} "
        f"petal_sacrificed={petal_gy} vial_on_bf={vial_bf}")

    # A6: cleanup
    post_wf = wf_type(post_s) if post_s else None
    A["A6_cleanup"] = "passed" if (
        post_s and post_wf not in ("ManaSourceSelection",)
        and not (post_s.get("stack") or [])) else (
        "failed" if post_s else "not-run")
    D["A6_cleanup_detail"] = (f"post waiting_for={post_wf} "
                              f"stack_empty={not (post_s.get('stack') or []) if post_s else None}")

    if A["A1_setup_ok"] == "failed":
        verdict = "blocked"
    elif (A["A2_selection_prompted"] == "passed"
          and A["A3_options_carry_color"] == "passed"
          and A["A4_ui_drops_color"] == "passed"):
        verdict = "reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "waiting_for_seq": [w[0] for w in WF_SEEN],
                  "rejections": ST["rejections"]}
    json.dump(assertions, open(f"{EVDIR}/assertions.json", "w"), indent=1)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("final", assertions)
    await C0.close()
    await C1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
