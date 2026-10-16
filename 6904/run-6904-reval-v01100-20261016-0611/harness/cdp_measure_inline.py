#!/usr/bin/env python3
"""CDP geometry test for phase-rs/phase#6904 (mana pips size on hand cards).

Renders the REAL ManaCostPips (size="fluid") inside the REAL hand-card
@container overlay structure from PlayerHand.tsx, with the REAL
handFanGeometry wide-profile overlap margins and zIndex=index stacking, at
real responsive --hand-card-w sizes. Measures via CDP whether the rendered
pips (a) stay within the 32cqi width budget that clears the card name,
(b) anchor over the printed-cost region, (c) stay legible in px, and
(d) are not covered by the next card in the fan.

The harness is served as inlined HTML (Page.setDocumentContent) because this
box's Chromium cannot reach loopback HTTP servers.

Usage: cdp_measure_inline.py <harness_dir> <out_dir>
"""
import asyncio
import base64
import glob
import json
import os
import sys
import time
import urllib.request

CDP_PORT = 9333

CONFIGS = [
    ("w1920_single5", 1920, 1080, "single5"),
    ("w1920_pair8", 1920, 1080, "pair8"),
    ("w1920_fan7", 1920, 1080, "fan7"),
    ("w1920_fan2", 1920, 1080, "fan2"),
    ("w1366_single5", 1366, 768, "single5"),
    ("w1366_fan7", 1366, 768, "fan7"),
    ("w844_single5", 844, 499, "single5"),
    ("w844_fan7", 844, 499, "fan7"),
]


class CDP:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.mid = 0
        self.pending = {}

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.ws_url, max_size=50_000_000)
        self.pump_task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *a):
        self.pump_task.cancel()
        await self.ws.close()

    async def _pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending[m["id"]].set_result(m)

    async def call(self, method, params=None, timeout=60):
        self.mid += 1
        mid = self.mid
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)

    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        rr = r["result"]["result"]
        return rr.get("value", rr.get("description"))

    async def shot(self, path):
        r = await self.call("Page.captureScreenshot", {"format": "png"})
        open(path, "wb").write(base64.b64decode(r["result"]["data"]))
        print("screenshot ->", path, flush=True)


def new_tab():
    body = json.dumps({"url": "about:blank"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}/json/new", data=body,
                                 headers={"Content-Type": "application/json"}, method="PUT")
    return json.load(urllib.request.urlopen(req))


def close_tab(tab_id):
    urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab_id}")


def build_html(harness_dir, cfg):
    dist = os.path.join(harness_dir, "harness-dist", "assets")
    js = glob.glob(os.path.join(dist, "index-*.js"))[0]
    css = glob.glob(os.path.join(dist, "style-*.css"))[0]
    js_text = open(js).read()
    css_text = open(css).read()
    # neutralize any stray absolute asset refs (none expected in this bundle)
    return f"""<!doctype html><html><head><meta charset="UTF-8">
<script>window.__CFG__={json.dumps(cfg)};</script>
<style>{css_text}</style></head>
<body><div id="root"></div>
<script type="module">{js_text}</script>
</body></html>"""


def intersect_area(a, b):
    if not a or not b:
        return 0.0
    x1, x2 = max(a["left"], b["left"]), min(a["right"], b["right"])
    y1, y2 = max(a["top"], b["top"]), min(a["bottom"], b["bottom"])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


async def run_config(_unused, html, out_dir, name, w, h, cfg):
    tab = new_tab()
    try:
        async with CDP(tab["webSocketDebuggerUrl"]) as c:
            await c.call("Page.enable")
            await c.call("Runtime.enable")
            await c.call("Emulation.setDeviceMetricsOverride",
                         {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
            await c.call("Page.setDocumentContent", {"frameId": tab["id"], "html": html})
            for _ in range(60):
                ready = await c.ev("window.__ready === true ? 1 : 0")
                if ready:
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError(f"{name}: harness never became ready")
            await asyncio.sleep(1.0)
            geom = await c.ev("window.__geom()")
            await c.shot(f"{out_dir}/shot_{name}.png")
    finally:
        close_tab(tab["id"])
    return {"name": name, "viewport": {"w": w, "h": h}, "cfg": cfg, "geom": geom}


def check(cond, observed):
    return {"status": "passed" if cond else "failed", "observed": observed}


async def main():
    harness_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for name, w, h, cfg in CONFIGS:
        print(f"--- {name} ({w}x{h}, {cfg}) ---", flush=True)
        html = build_html(harness_dir, cfg)
        r = await run_config(None, html, out_dir, name, w, h, cfg)
        for c in r["geom"]["cards"]:
            f, b = c["frame"], c["badge"]
            frac = (b["width"] / f["width"]) if b and f else None
            print(f"  card{c['index']}: frame_w={f['width']:.1f} "
                  f"badge_w={b['width']:.1f} ({frac:.3f} of frame) "
                  f"pips={c['pipCount']} handCardW={r['geom']['handCardW'].strip()}", flush=True)
        results.append(r)

    by_cfg = {}
    for r in results:
        by_cfg.setdefault(r["cfg"], []).append(r)

    assertions = {}

    # A1: name clearance — badge stays within the 32cqi budget (<=34% incl.
    # backdrop insets), its left edge right of the 55% line, and it never
    # intersects the printed-name strip.
    a1_lines, a1_ok = [], True
    for r in results:
        for c in r["geom"]["cards"]:
            f, b, n = c["frame"], c["badge"], c["name"]
            wfrac = b["width"] / f["width"]
            leftfrac = (b["left"] - f["left"]) / f["width"]
            name_ov = intersect_area(b, n)
            ok = wfrac <= 0.34 and leftfrac >= 0.55 and name_ov <= 0.5
            a1_ok = a1_ok and ok
            if r["cfg"] in ("single5", "pair8"):
                a1_lines.append(
                    f"{r['name']}: badge={wfrac:.3f}w left@{leftfrac:.3f} name_overlap={name_ov:.2f}px2 -> {'ok' if ok else 'FAIL'}")
    assertions["A1_name_clearance"] = check(a1_ok, "; ".join(a1_lines))

    # A2: anchor over the printed-cost region (right 6.5% / top 5% of width).
    a2_lines, a2_ok = [], True
    for r in results:
        for c in r["geom"]["cards"]:
            f, b = c["frame"], c["badge"]
            rightfrac = (f["right"] - b["right"]) / f["width"]
            topfrac = (b["top"] - f["top"]) / f["width"]
            ok = 0.05 <= rightfrac <= 0.08 and 0.03 <= topfrac <= 0.07
            a2_ok = a2_ok and ok
            if c["index"] == 0:
                a2_lines.append(f"{r['name']}: right@{rightfrac:.3f}w top@{topfrac:.3f}w -> {'ok' if ok else 'FAIL'}")
    assertions["A2_printed_cost_anchor"] = check(a2_ok, "; ".join(a2_lines))

    # A3: the 8-symbol split pair holds the same width budget (shrink tiers).
    a3_lines, a3_ok = [], True
    for r in by_cfg.get("pair8", []):
        c = r["geom"]["cards"][0]
        f, b = c["frame"], c["badge"]
        wfrac = b["width"] / f["width"]
        ok = wfrac <= 0.34 and c["pipCount"] == 8
        a3_ok = a3_ok and ok
        a3_lines.append(f"{r['name']}: badge={wfrac:.3f}w pips={c['pipCount']} -> {'ok' if ok else 'FAIL'}")
    assertions["A3_pair_width_budget"] = check(a3_ok, "; ".join(a3_lines))

    # A4: pip disks stay a legible size in px at every tested scale.
    a4_lines, a4_ok = [], True
    min_d = 1e9
    for r in results:
        ds = [min(p["width"], p["height"]) for c in r["geom"]["cards"] for p in c["pips"]]
        if ds:
            min_d = min(min_d, min(ds))
            if min(ds) < 6.0:
                a4_ok = False
        a4_lines.append(f"{r['name']}: min_pip_d={min(ds):.1f}px")
    assertions["A4_pip_legibility_px"] = check(
        a4_ok, f"global min pip diameter={min_d:.1f}px (floor 6px); " + "; ".join(a4_lines))

    # A5: the next card in the fan (higher z-index, slid left over this card's
    # right portion) must not cover this card's pip badge.
    a5_lines, a5_ok = [], True
    for r in results:
        if r["cfg"] not in ("fan7", "fan2"):
            continue
        cards = r["geom"]["cards"]
        for j in range(len(cards) - 1):
            f_next = cards[j + 1]["frame"]
            b = cards[j]["badge"]
            ov = intersect_area(b, f_next)
            ok = ov <= 0.5
            a5_ok = a5_ok and ok
            a5_lines.append(f"{r['name']}: card{j + 1} covers card{j} badge by {ov:.1f}px2 -> {'ok' if ok else 'COVERED'}")
    assertions["A5_no_front_card_cover"] = check(a5_ok, "; ".join(a5_lines))

    out = {"configs": results, "assertions": assertions,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(f"{out_dir}/ui_observations.json", "w") as f:
        json.dump(out, f, indent=1)
    print("assertions:", json.dumps(assertions, indent=1), flush=True)


asyncio.run(main())
