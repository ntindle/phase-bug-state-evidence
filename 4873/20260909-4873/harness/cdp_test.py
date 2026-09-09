#!/usr/bin/env python3
"""CDP filter-latency test for NamedChoiceModal (phase-rs/phase#4873 <- #4832).

Drives the real NamedChoiceModal (mainline or PR-head variant) in headless
Chromium via CDP:
  1. loads the modal with 384 creature-type options (Sliver at index 306),
  2. 1.0s after mount types "sliver" into the filter input,
  3. polls the Sliver pill's computed opacity until it reaches 1,
  4. selects Sliver + Confirm and records the dispatched payload.

Usage: cdp_test.py <page_url> <variant> <out_prefix>
"""
import asyncio
import base64
import json
import sys
import time
import urllib.request

CDP_PORT = 9333

STATE_JS = """(() => {
  const pills = [...document.querySelectorAll('[data-testid="choice-overlay"] button')].filter(
    b => b.dataset.testid !== 'confirm' && b.textContent.trim().length > 0
  );
  const info = pills.map(b => {
    const r = b.getBoundingClientRect();
    return {
      text: b.textContent.trim(),
      opacity: parseFloat(getComputedStyle(b).opacity || '0'),
      cx: r.x + r.width / 2, cy: r.y + r.height / 2,
    };
  });
  const filter = document.querySelector('input[placeholder="Filter options..."]');
  return {
    pillCount: pills.length,
    pills: info,
    filterPresent: !!filter,
    payloads: window.__payloads || [],
  };
})()"""

OPACITY_JS = """(() => {
  const pills = [...document.querySelectorAll('[data-testid="choice-overlay"] button')].filter(
    b => b.dataset.testid !== 'confirm' && b.textContent.trim().length > 0
  );
  const byText = {};
  for (const b of pills) byText[b.textContent.trim()] = parseFloat(getComputedStyle(b).opacity || '0');
  return { count: pills.length, byText };
})()"""

TYPE_FILTER_JS = """(() => {
  const filter = document.querySelector('input[placeholder="Filter options..."]');
  if (!filter) return 'NO-FILTER';
  filter.focus();
  return 'focused';
})()"""

CLICK_SLIVER_JS = """(() => {
  const pills = [...document.querySelectorAll('[data-testid="choice-overlay"] button')].filter(
    b => b.dataset.testid !== 'confirm' && b.textContent.trim() === 'Sliver'
  );
  if (!pills.length) return 'NO-SLIVER';
  pills[0].click();
  return 'clicked';
})()"""

CLICK_CONFIRM_JS = """(() => {
  const b = document.querySelector('[data-testid="confirm"]');
  if (!b) return 'NO-CONFIRM';
  b.click();
  return 'clicked';
})()"""


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


async def main():
    url, variant, out = sys.argv[1], sys.argv[2], sys.argv[3]
    result = {"variant": variant, "url": url}
    tab = new_tab()
    try:
        async with CDP(tab["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Page.navigate", {"url": url})

            # wait for the modal to mount
            t_mount = None
            for _ in range(60):
                ready = await cdp.ev("document.querySelector('[data-testid=\"overlay-title\"]') ? 1 : 0")
                if ready:
                    t_mount = time.monotonic()
                    break
                await asyncio.sleep(0.25)
            assert t_mount is not None, "modal never mounted"
            result["t_mount_wall"] = t_mount

            # A1/A2 sanity: pill count, filter input, stagger presence
            await asyncio.sleep(0.6)
            st = await cdp.ev(STATE_JS)
            result["sanity"] = {
                "pillCount": st["pillCount"],
                "filterPresent": st["filterPresent"],
                "firstPillOpacity": st["pills"][0]["opacity"] if st["pills"] else None,
                "lastPillOpacity": st["pills"][-1]["opacity"] if st["pills"] else None,
                "lastPillText": st["pills"][-1]["text"] if st["pills"] else None,
            }
            print("sanity:", json.dumps(result["sanity"]), flush=True)

            # A3: at ~1.0s after mount (a fast-typing user), filter for "sliver"
            await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - t_mount)))
            t_type = time.monotonic()
            focused = await cdp.ev(TYPE_FILTER_JS)
            assert focused == "focused", f"filter focus failed: {focused}"
            # faithful keystroke-equivalent: insertText dispatches beforeinput/input
            await cdp.call("Input.insertText", {"text": "sliver"})
            result["t_type_wall"] = t_type
            await asyncio.sleep(0.3)
            await cdp.shot(f"{out}_typed.png")

            # poll the Sliver pill's opacity until visible
            t_visible = None
            samples = []
            deadline = t_type + 16.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                o = await cdp.ev(OPACITY_JS)
                sl = o["byText"].get("Sliver")
                samples.append({"t": round(now - t_type, 2), "count": o["count"], "sliverOpacity": sl})
                if sl is not None and sl >= 0.99:
                    t_visible = now
                    break
                await asyncio.sleep(0.05)
            result["samples"] = samples
            result["latency_after_type_s"] = round(t_visible - t_type, 2) if t_visible else None
            result["latency_after_mount_s"] = round((t_visible or deadline) - t_mount, 2)
            print("latency_after_type_s:", result["latency_after_type_s"], flush=True)
            await cdp.shot(f"{out}_visible.png")

            # A4: select Sliver + Confirm, record dispatch payload
            clicked = await cdp.ev(CLICK_SLIVER_JS)
            result["sliver_click"] = clicked
            await asyncio.sleep(0.4)
            conf = await cdp.ev(CLICK_CONFIRM_JS)
            result["confirm_click"] = conf
            await asyncio.sleep(0.6)
            st2 = await cdp.ev(STATE_JS)
            result["payloads"] = st2["payloads"]
            await cdp.shot(f"{out}_final.png")
    finally:
        close_tab(tab["id"])

    with open(f"{out}_result.json", "w") as f:
        json.dump(result, f, indent=1)
    print("wrote", f"{out}_result.json", flush=True)


asyncio.run(main())
