#!/usr/bin/env python3
"""CDP driver: load a harness page, read modal badge state, screenshot."""
import asyncio
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
    return { name: el.textContent.trim(), badge, domOrder: null };
  });
  cards.forEach((c, i) => c.domOrder = i);
  const payload = document.getElementById('payload') ? document.getElementById('payload').innerText : null;
  const title = document.querySelector('[data-testid="overlay-title"]');
  return { title: title ? title.textContent : null, cards, payload };
})()"""


async def main():
    url, out_prefix = sys.argv[1], sys.argv[2]
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json"))
    ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]

    import websockets
    msg_id = 0
    pending = {}

    async def send(ws, method, params=None):
        nonlocal msg_id
        msg_id += 1
        await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        return msg_id

    async with websockets.connect(ws_url, max_size=10_000_000) as ws:
        async def call(method, params=None, timeout=20):
            mid = await send(ws, method, params)
            async def waiter():
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("id") == mid:
                        return m
            return await asyncio.wait_for(waiter(), timeout)

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Page.navigate", {"url": url})
        # wait for overlay title
        for _ in range(40):
            r = await call("Runtime.evaluate", {"expression": "document.querySelector('[data-testid=\"overlay-title\"]') ? document.querySelector('[data-testid=\"overlay-title\"]').textContent : null", "returnByValue": True})
            if r["result"]["result"]["value"]:
                break
            await asyncio.sleep(0.5)
        state = await call("Runtime.evaluate", {"expression": READ_STATE_JS, "returnByValue": True})
        print("STATE:", json.dumps(state["result"]["result"]["value"], indent=1))
        shot = await call("Page.captureScreenshot", {"format": "png"})
        import base64
        open(f"{out_prefix}.png", "wb").write(base64.b64decode(shot["result"]["data"]))
        print("screenshot ->", f"{out_prefix}.png")

asyncio.run(main())
