#!/usr/bin/env python3
"""CDP geometry test for phase-rs/phase#6859.

Drives the REAL TargetingOverlay + REAL OpponentHud (current mainline client)
in headless Chromium via CDP:

  - seeds a TargetSelection for Ajani, Nacatl Avenger's 0 ability
    ("...deals damage ... to any target"), the exact prompt from the report;
  - places the opponent HUD with its top edge at the real layout's rail top
    (top-band bottom edge, via the real useResolvedGridRows());
  - measures the prompt bar rect vs the opponent life-total rect at nine
    viewport/band configurations and asserts they do not overlap.

Usage: cdp_measure.py <base_url> <out_dir>
"""
import asyncio
import base64
import json
import sys
import time
import urllib.request

CDP_PORT = 9333

CONFIGS = [
    ("v_1920x1080", 1920, 1080, "default"),
    ("v_1440x900", 1440, 900, "default"),
    ("v_1280x720", 1280, 720, "default"),
    ("v_1024x768", 1024, 768, "default"),
    ("v_1024x600", 1024, 600, "default"),
    ("v_390x844", 390, 844, "default"),
    ("v_844x499", 844, 499, "default"),
    ("v_1024x600_layout2", 1024, 600, "layout2"),
    ("v_844x499_layout2", 844, 499, "layout2"),
]


class CDP:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.mid = 0

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.ws_url, max_size=10_000_000)
        return self

    async def __aexit__(self, *a):
        await self.ws.close()

    async def call(self, method, params=None, timeout=30):
        self.mid += 1
        mid = self.mid
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        async def waiter():
            async for raw in self.ws:
                m = json.loads(raw)
                if m.get("id") == mid:
                    if "error" in m:
                        raise RuntimeError(f"CDP {method}: {m['error']}")
                    return m
        return await asyncio.wait_for(waiter(), timeout)

    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"]["result"]["value"]

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


def overlap(a, b):
    if not a or not b:
        return None
    x1, x2 = max(a["left"], b["left"]), min(a["right"], b["right"])
    y1, y2 = max(a["top"], b["top"]), min(a["bottom"], b["bottom"])
    return {"x": round(max(0.0, x2 - x1), 2), "y": round(max(0.0, y2 - y1), 2)}


async def run_config(cdp, base_url, out_dir, name, w, h, band):
    if base_url.startswith("file://"):
        url = base_url.split("?")[0] + f"?band={band}"
    else:
        url = f"{base_url}?band={band}"
    await cdp.call("Emulation.setDeviceMetricsOverride",
                   {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
    await cdp.call("Page.navigate", {"url": url})
    # wait for the harness to mount + settle animations
    for _ in range(60):
        ready = await cdp.ev("window.__ready === true ? 1 : 0")
        if ready:
            break
        await asyncio.sleep(0.25)
    else:
        raise RuntimeError(f"{name}: harness never became ready")
    await asyncio.sleep(1.5)
    geom = await cdp.ev("window.__geom()")
    await cdp.shot(f"{out_dir}/shot_{name}.png")
    ov = overlap(geom.get("pill"), geom.get("life"))
    hud_ov = overlap(geom.get("pill"), geom.get("hud"))
    return {
        "name": name, "viewport": {"w": w, "h": h}, "band": band,
        "geom": geom,
        "pill_life_overlap": ov,
        "pill_hud_overlap": hud_ov,
        "pill_clears_life": ov is not None and (ov["x"] <= 0.5 or ov["y"] <= 0.5),
    }


async def main():
    base_url, out_dir = sys.argv[1], sys.argv[2]
    tab = new_tab()
    results = []
    try:
        async with CDP(tab["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            for name, w, h, band in CONFIGS:
                print(f"--- {name} ({w}x{h}, {band}) ---", flush=True)
                r = await run_config(cdp, base_url, out_dir, name, w, h, band)
                g = r["geom"]
                print("  prompt:", (g.get("promptText") or "")[:60], flush=True)
                print("  pill:", g.get("pill"), "life:", g.get("life"), flush=True)
                print("  pill/life overlap:", r["pill_life_overlap"], "clears:", r["pill_clears_life"], flush=True)
                print("  chooseButtons:", g.get("chooseButtons"), flush=True)
                results.append(r)
    finally:
        close_tab(tab["id"])

    # assertions
    assertions = {}
    first = results[0]["geom"]
    assertions["A1_prompt_rendered"] = {
        "status": "passed" if first.get("pill") and first.get("promptText") else "failed",
        "observed": f"pill={first.get('pill')} prompt={(first.get('promptText') or '')[:80]}",
    }
    std = [r for r in results if r["band"] == "default"]
    std_clear = all(r["pill_clears_life"] for r in std)
    assertions["A2_no_overlap_standard_viewports"] = {
        "status": "passed" if std_clear else "failed",
        "observed": "; ".join(
            f"{r['name']}: overlap={r['pill_life_overlap']} clears={r['pill_clears_life']}"
            for r in std),
    }
    l2 = [r for r in results if r["band"] == "layout2"]
    assertions["A3_layout2_viewports"] = {
        "status": "passed" if all(r["pill_clears_life"] for r in l2) else "failed",
        "observed": "; ".join(
            f"{r['name']}: overlap={r['pill_life_overlap']} clears={r['pill_clears_life']}"
            for r in l2),
        "note": "layout2 844x499 is the documented #7699 residual cell",
    }
    assertions["A4_player_target_controls"] = {
        "status": "passed" if (first.get("chooseButtons") or 0) >= 2 else "failed",
        "observed": f"chooseButtons={first.get('chooseButtons')} (both players offered as direct targets)",
    }
    out = {"configs": results, "assertions": assertions,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(f"{out_dir}/ui_observations.json", "w") as f:
        json.dump(out, f, indent=1)
    print("assertions:", json.dumps(assertions, indent=1), flush=True)


asyncio.run(main())
