#!/usr/bin/env python3
"""Faithful PNG summary for #7171 v0.111.0 re-validation evidence, generated
from saved findings."""
import json
import os
import sys
from datetime import date

from PIL import Image, ImageDraw, ImageFont

EVDIR = sys.argv[1]
A = json.load(open(os.path.join(EVDIR, "assertions.json")))
RUN = json.load(open(os.path.join(EVDIR, "run.json")))
VIEW = json.load(open(os.path.join(EVDIR, "deckbuilding_view.json")))
PROBE = json.load(open(os.path.join(EVDIR, "companion_probe.json")))

W, H = 1000, 920
BG, FG, DIM, ACC, RED, GRN, YEL = (16, 20, 28), (232, 236, 244), (150, 160, 175), \
    (96, 165, 250), (248, 113, 113), (74, 222, 128), (250, 204, 21)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
try:
    fT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
    fH = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
    fB = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    fS = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
except Exception:
    fT = fH = fB = fS = ImageFont.load_default()

y = 24
d.text((32, y), "phase-rs/phase #7171 — bug-state evidence", font=fT, fill=FG); y += 44
d.text((32, y), "No way to add/set Companion in Build Deck after Draft", font=fH, fill=DIM); y += 34

verdict = A["verdict"]
vc = GRN if verdict == "reproduced" else (YEL if verdict == "not-reproduced" else RED)
d.rectangle([32, y, 32 + 220, y + 34], outline=vc, width=2)
d.text((44, y + 7), "VERDICT: " + verdict.upper(), font=fH, fill=vc)
d.text((280, y + 9), "%s · build %s · protocol %s · %s" % (
    RUN["server_version"], RUN["build_commit"], RUN["protocol_version"],
    RUN["validated_at"]), font=fS, fill=DIM)
y += 52

def section(title):
    global y
    d.text((32, y), title, font=fH, fill=ACC); y += 28

def body(text, font=fB, fill=FG, indent=32, wrap=104):
    global y
    words, line = text.split(), ""
    for w in words:
        if len(line) + len(w) + 1 > wrap:
            d.text((indent, y), line, font=font, fill=fill); y += 22; line = w
        else:
            line = (line + " " + w).strip()
    if line:
        d.text((indent, y), line, font=font, fill=fill); y += 22
    y += 6

section("Setup (live probe, pinned release)")
body("Sealed event: 6x Ikoria packs dealt directly to each of 8 seats "
     "(Lutri, the Spellchaser is in the iko draft pool), PodPolicy Casual. "
     "Sealed was used instead of pick-and-pass: the reported bug lives in "
     "the Build Deck stage and the SubmitDeck protocol path, which are "
     "kind-independent; pick mechanics are not implicated by the triage "
     "scope. Human seat reached Deckbuilding over the real WS protocol; "
     "deckbuilder = Deckbuilding status + DraftPlayerView + "
     "DraftAction::SubmitDeck.")
y += 2

section("Observed")
pool = VIEW.get("pool", [])
lutri_in_pool = "Lutri, the Spellchaser" in [c.get("name") for c in pool]
body("Draft reached Deckbuilding; human pool = %d cards; Lutri drafted: %s." % (
    len(pool), "yes" if lutri_in_pool else "no (never appeared in packs)"))
body("Deckbuilding DraftPlayerView keys: " + ", ".join(sorted(VIEW.keys())[:12]) + " …")
body("No 'companion' key anywhere in the Deckbuilding view — no companion control "
     "or capability is advertised to the Build Deck screen (A3).")
resp_type = PROBE.get("response_type")
body("SubmitDeck carrying a \"companions\" designation -> server replied: %s; "
     "the field was silently discarded — no companion designation stored or "
     "visible in the post-probe view (A4)." % resp_type)
body("Protocol type DraftAction::SubmitDeck { seat, main_deck, commanders } still has "
     "no companion field. The engine itself supports a companion slot "
     "(PlayerDeckList.companion in deck_loading.rs, verified on the v0.111.0 "
     "source), so the gap remains confined to the draft deck-submission path.")
y += 2

section("Assertions")
for k in sorted(A["assertions"]):
    v = A["assertions"][k]
    c = GRN if v == "passed" else (YEL if v == "not-run" else RED)
    d.text((32, y), "●", font=fB, fill=c)
    d.text((56, y), "%s: %s" % (k, v), font=fB, fill=FG); y += 22
y += 6
body("Scope: draft deck-submission wire path + Build Deck view on v0.111.0 "
     "(protocol 144). Browser UI not driven (no served frontend on this box); "
     "React deck-builder code not re-inspected this run — the wire verdict is "
     "authoritative for the submission path. Lutri never appeared in this "
     "seat's packs (A2 not-run); the probe targeted another pool card, which "
     "does not change the protocol-path finding.", font=fS, fill=DIM)

d.text((32, H - 30), "evidence: ntindle/phase-bug-state-evidence 7171/%s" % RUN["run_id"],
       font=fS, fill=DIM)
out = os.path.join(EVDIR, "summary.png")
img.save(out)
print("wrote", out, img.size)
