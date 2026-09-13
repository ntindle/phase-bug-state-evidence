#!/usr/bin/env python3
"""Debug helper: load a URL in headless Chromium via CDP, print console errors
and exceptions, then report window.__ready and #root contents."""
import asyncio, json, sys, urllib.request, websockets

CDP_PORT = 9333

async def main(url):
    body = json.dumps({"url": "about:blank"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{CDP_PORT}/json/new", data=body,
                                 headers={"Content-Type": "application/json"}, method="PUT")
    tab = json.load(urllib.request.urlopen(req))
    mid = 0
    pending = {}
    events = []
    async def pump(ws):
        async for raw in ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in pending:
                pending[m["id"]].set_result(m)
            else:
                events.append(m)
    async def call(ws, method, params=None, timeout=30):
        nonlocal mid
        mid += 1
        fut = asyncio.get_running_loop().create_future()
        pending[mid] = fut
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)
    async def ev(ws, expr):
        r = await call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        rr = r["result"]["result"]
        return rr.get("value", rr.get("description"))
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=10_000_000) as ws:
            pump_task = asyncio.create_task(pump(ws))
            await call(ws, "Page.enable"); await call(ws, "Runtime.enable")
            await call(ws, "Page.navigate", {"url": url})
            await asyncio.sleep(7)
            print("ready:", await ev(ws, "window.__ready"))
            print("root:", await ev(ws, "document.getElementById('root') ? document.getElementById('root').innerHTML.length : 'NO ROOT'"))
            for e in events:
                meth = e.get("method")
                if meth == "Runtime.exceptionThrown":
                    d = e["params"]["exceptionDetails"]
                    print("EXCEPTION:", d.get("text"), str(d.get("exception", {}).get("description", ""))[:400])
                elif meth == "Runtime.consoleAPICalled":
                    args = [a.get("value", a.get("description")) for a in e["params"].get("args", [])]
                    if e["params"]["type"] in ("error", "warning"):
                        print("CONSOLE", e["params"]["type"], ":", str(args)[:400])
            pump_task.cancel()
    finally:
        urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab['id']}")

asyncio.run(main(sys.argv[1]))
