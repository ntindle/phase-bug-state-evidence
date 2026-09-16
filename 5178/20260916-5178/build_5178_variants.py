#!/usr/bin/env python3
"""Build runnable JS variants of parseJoinCode for issue #5178 validation.

Extracts the REAL parseJoinCode from the PR-base source file
(client/src/services/serverDetection.ts @ base commit d6197d0d, blob
27177fbf2095a5d7ad1eefd5fdbad82519abdc5c), strips TS types deterministically,
and emits:
  - variant_mainline.js : verbatim base implementation
  - variant_pr.js       : PR head's implementation (client/src/services/serverDetection.ts
                          @ PR head bfc031af, blob 35198cbcdcb560e61b2901d99df0d8666ec0899b)

Both variants are functionally identical to their sources modulo type erasure.
The PR variant is built from the PR head file directly (not a re-typed patch),
so it is byte-identical to the PR's published change modulo type erasure.
"""
import re, sys

SRC = "/home/hatch/workspace/dev/phase-backfill/driver/5178_serverDetection_base.ts"
PR_HEAD = "/tmp/pr_head_serverDetection.ts"
OUTDIR = "/home/hatch/workspace/dev/phase-backfill/driver"

src = open(SRC).read()

# Extract parseJoinCode: from its `export function` line up to (not incl.) the
# doc comment of formatJoinShare that follows it.
m = re.search(
    r"export function parseJoinCode\(input: string\): \{ code: string; serverAddress\?: string \} \{(.*?)\n\}\n\n/\*\*",
    src, re.S)
assert m, "parseJoinCode body not found (base)"
base_body = m.group(1)

mh = re.search(
    r"export function parseJoinCode\(input: string\): \{ code: string; serverAddress\?: string \} \{(.*?)\n\}\n\n/\*\*",
    open(PR_HEAD).read(), re.S)
assert mh, "parseJoinCode body not found (PR head)"
pr_body = mh.group(1)

# Confirm the head differs from base exactly by the published patch.
import difflib
d = list(difflib.unified_diff(base_body.splitlines(), pr_body.splitlines(), lineterm=""))
print("\n".join(d))

def to_js(fn_name, body):
    js = "function %s(input) {\n%s\n}\nmodule.exports = { %s };\n" % (fn_name, body, fn_name)
    # type erasure
    js = js.replace('let explicitScheme: "ws" | "wss" | null = null;',
                    'let explicitScheme = null;')
    js = js.replace('let port: number;', 'let port;')
    js = "const DEFAULT_PORT = 9374;\n" + js
    return js

mainline = to_js("parseJoinCode", base_body)
pr = to_js("parseJoinCode", pr_body)

# sanity: no TS remnants
for label, code in (("mainline", mainline), ("pr", pr)):
    for bad in (": string", ": number", "| null", "?:"):
        assert bad not in code, f"{label} still has TS: {bad!r}"

open(f"{OUTDIR}/variant_mainline.js", "w").write(mainline)
open(f"{OUTDIR}/variant_pr.js", "w").write(pr)
print("variants written")
