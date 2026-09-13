#!/usr/bin/env python3
"""CDP frontend-leg test for phase-rs/phase#7028.

Renders the REAL OptionalEffectModalContent (the component GamePage renders
for the Bre "you may cast / otherwise hand" OptionalEffectChoice) with the
REAL captured waiting_for payload + REAL game objects from the engine leg,
then measures via CDP whether the exiled nonland card's identity is displayed
anywhere in the rendered modal.

Verdict rule (per scenario contract): reproduced iff the exiled card is NOT
displayed in the modal.

Usage: cdp_7028.py <harness_dir> <payload_json> <out_dir>
"""
import asyncio
import base64
import glob
import json
import os
import sys
import urllib.request

CDP_PORT = 9333


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
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                fut = self.pending.get(m.get("id"))
                if fut is not None and not fut.done():
                    fut.set_result(m)
        except Exception:
            pass

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


def build_html(harness_dir, payload):
    dist = os.path.join(harness_dir, "harness-dist", "assets")
    js = glob.glob(os.path.join(dist, "index-*.js"))[0]
    css = glob.glob(os.path.join(dist, "style-*.css"))[0]
    js_text = open(js).read()
    css_text = open(css).read()
    return f"""<!doctype html><html><head><meta charset="UTF-8">
<script>window.__PAYLOAD__={json.dumps(payload)};</script>
<script>
// about:blank via Page.setDocumentContent is a non-secure context, so
// crypto.randomUUID is missing; the real app always runs on https/localhost.
if (typeof crypto !== "undefined" && typeof crypto.randomUUID !== "function") {{
  crypto.randomUUID = function () {{
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {{
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    }});
  }};
}}
</script>
<style>{css_text}</style></head>
<body><div id="root"></div>
<script type="module">{js_text}</script>
</body></html>"""


async def main():
    harness_dir, payload_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)
    payload = json.load(open(payload_path))
    html = build_html(harness_dir, payload)
    tab = new_tab()
    try:
        async with CDP(tab["webSocketDebuggerUrl"]) as c:
            await c.call("Page.enable")
            await c.call("Runtime.enable")
            await c.call("Emulation.setDeviceMetricsOverride",
                         {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
            await c.call("Page.setDocumentContent", {"frameId": tab["id"], "html": html})
            for _ in range(60):
                ready = await c.ev("window.__ready === true ? 1 : 0")
                if ready:
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("harness never became ready")
            await asyncio.sleep(2.0)
            geom = await c.ev("window.__geom()")
            await c.shot(f"{out_dir}/frontend_modal.png")
    finally:
        close_tab(tab["id"])

    with open(f"{out_dir}/frontend_measure.json", "w") as f:
        json.dump(geom, f, indent=1)
    print(json.dumps(geom, indent=1)[:3000])

    exiled = payload["exiled_name"]
    shown = geom.get("exiledNameVisibleInModal") is True
    print(f"\nexiled card: {exiled}")
    print(f"displayed in modal: {shown}")
    print("FRONTEND VERDICT:", "not-reproduced (card IS shown)" if shown else "REPRODUCED (card NOT shown)")
    return 0 if not geom.get("renderError") else 1


if __name__ == "__main__":
    asyncio.run(main())
