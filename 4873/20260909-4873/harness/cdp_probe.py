#!/usr/bin/env python3
"""Probe: load a harness page, print body HTML / vite error overlay text."""
import asyncio, json, sys, urllib.request

CDP_PORT = 9333

class CDP:
    def __init__(self, ws_url):
        self.ws_url = ws_url; self.mid = 0
    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.ws_url, max_size=10_000_000)
        return self
    async def __aexit__(self, *a):
        await self.ws.close()
    async def call(self, method, params=None, timeout=30):
        self.mid += 1; mid = self.mid
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        async def waiter():
            async for raw in self.ws:
                m = json.loads(raw)
                if m.get("id") == mid:
                    if "error" in m: raise RuntimeError(f"CDP {method}: {m['error']}")
                    return m
        return await asyncio.wait_for(waiter(), timeout)
    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"]["result"]["value"]

def new_tab():
    body = json.dumps({"url": "about:blank"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}/json/new", data=body,
                                 headers={"Content-Type": "application/json"}, method="PUT")
    return json.load(urllib.request.urlopen(req))

def close_tab(tab_id):
    urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab_id}")

async def probe(url):
    tab = new_tab()
    try:
        async with CDP(tab["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable"); await cdp.call("Runtime.enable")
            await cdp.call("Page.navigate", {"url": url})
            await asyncio.sleep(5)
            ov = await cdp.ev("(() => { const o = document.querySelector('vite-error-overlay'); return o ? o.shadowRoot.textContent.slice(0,3000) : null; })()")
            print("OVERLAY:", ov if ov else "(none)")
            print("BODY:", (await cdp.ev("document.getElementById('root') ? document.getElementById('root').innerHTML.slice(0,300) : 'no-root'")))
    finally:
        close_tab(tab["id"])

if __name__ == "__main__":
    asyncio.run(probe(sys.argv[1]))
