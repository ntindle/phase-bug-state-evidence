#!/usr/bin/env python3
"""CDP interaction test for the scry reorder harness.
Usage: cdp_test.py <page_url> <mode> <out_prefix>
  mode = drag   : drag 2nd card left, read badges, click Confirm, read payload
  mode = arrows : click 'Island: Move later', read badges, Confirm, read payload
  mode = control: click Confirm with no reorder, read payload
"""
import asyncio
import base64
import json
import sys
import urllib.request

CDP_PORT = 9333

READ_STATE_JS = """(() => {
  const cards = [...document.querySelectorAll('[data-testid="card-image"]')].map(el => {
    const wrap = el.closest('div.relative');
    let badge = null;
    if (wrap) {
      const b = [...wrap.querySelectorAll('div')].find(d => d.className.includes('rounded-full') && /^\\d+$/.test(d.textContent.trim()));
      badge = b ? b.textContent.trim() : null;
    }
    const r = el.getBoundingClientRect();
    return { name: el.textContent.trim(), badge, cx: r.x + r.width/2, cy: r.y + r.height/2 };
  });
  cards.forEach((c, i) => c.domOrder = i);
  const payload = document.getElementById('payload') ? document.getElementById('payload').innerText : null;
  return { cards, payload };
})()"""

ARROW_NAMES_JS = """(() => [...document.querySelectorAll('button[aria-label]')].map(b => b.getAttribute('aria-label')))()"""


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

    async def call(self, method, params=None, timeout=25):
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
        print("screenshot ->", path)


async def main():
    url, mode, out = sys.argv[1], sys.argv[2], sys.argv[3]
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json"))
    ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
    result = {"mode": mode, "url": url}

    async with CDP(ws_url) as cdp:
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Page.navigate", {"url": url})
        for _ in range(40):
            t = await cdp.ev("document.querySelector('[data-testid=\"overlay-title\"]') ? 'ready' : null")
            if t:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.0)  # let framer-motion settle
        result["initial"] = await cdp.ev(READ_STATE_JS)
        await cdp.shot(f"{out}_initial.png")

        if mode == "drag":
            st = result["initial"]
            mtn = next(c for c in st["cards"] if c["name"] == "Mountain")
            isl = next(c for c in st["cards"] if c["name"] == "Island")
            sx, sy = mtn["cx"], mtn["cy"]
            tx = isl["cx"] - 20
            await cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1})
            steps = 25
            for i in range(1, steps + 1):
                x = sx + (tx - sx) * i / steps
                await cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": sy})
                await asyncio.sleep(0.02)
            await cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": tx, "y": sy, "button": "left", "clickCount": 1})
            await asyncio.sleep(1.5)
            result["after_drag"] = await cdp.ev(READ_STATE_JS)
            await cdp.shot(f"{out}_after_drag.png")
        elif mode == "arrows":
            result["arrow_buttons"] = await cdp.ev(ARROW_NAMES_JS)
            clicked = await cdp.ev("(() => { const b = document.querySelector('button[aria-label=\"Island: Move later\"]'); if (!b) return 'NOT-FOUND'; b.click(); return 'clicked'; })()")
            result["arrow_click"] = clicked
            await asyncio.sleep(1.0)
            result["after_arrows"] = await cdp.ev(READ_STATE_JS)
            await cdp.shot(f"{out}_after_arrows.png")

        # confirm
        clicked = await cdp.ev("(() => { const b = document.querySelector('[data-testid=\"confirm\"]'); if (!b) return 'NOT-FOUND'; b.click(); return 'clicked'; })()")
        result["confirm_click"] = clicked
        await asyncio.sleep(0.8)
        result["final"] = await cdp.ev(READ_STATE_JS)
        await cdp.shot(f"{out}_final.png")

    print("RESULT:", json.dumps(result, indent=1))
    open(f"{out}_result.json", "w").write(json.dumps(result, indent=1))

asyncio.run(main())
