# #9849 — blocked analysis: Coverage and semantic-audit JSON not byte-reproducible

Run: `run-9849-v01080-20261013-0812` (2026-10-13). Verdict: **blocked**.

## What was checked

1. **Release assets (v0.108.0, latest stable, published 2026-10-12T21:10:34Z).**
   The release publishes only `phase-server-slim` binaries (aarch64-apple-darwin,
   aarch64-unknown-linux-musl, x86_64-pc-windows-msvc.exe, x86_64-unknown-linux-musl),
   their `.minisig` files, `release-server-v0.108.0.json` (+ minisig) and `update.json`.
   No `coverage_report` / `oracle_gen` binaries are published.

2. **Local prebuilt binaries.** Searched `driver/`, `server/releases/*/`,
   `checkout-*/target/{debug,release}` for `coverage_report` / `oracle_gen`:
   none found. (A workspace-wide filename sweep was also run; result recorded in
   run.json `attempts` if it surfaced anything.)

3. **Source-level confirmation at the v0.108.0 tag** (so the preserved scenario is
   accurate when the tools become available):
   - `crates/engine/src/bin/oracle_gen.rs` dispatches a `semantic-audit` subcommand
     (`initial_args.get(1) == Some("semantic-audit")` → `run_semantic_audit`).
   - `crates/engine/src/bin/coverage_report.rs` supports `--brief` and builds its
     summary with `std::collections::HashMap`.

4. **Build feasibility on this VM:** no `cargo`/`rustc`; `free` shows 7 GB total,
   ~1 GB available, 0 swap. Not attempted (prior native engine linking exhausted
   memory on the original host; the playbook keeps this workflow on official
   release artifacts).

## Why blocked, not not-reproduced

The reported failure mode is a property of executing the two binaries twice over
identical inputs. Without the binaries there is no trustworthy observation to
make; a source-level reading confirms the code shapes the reporter describes but
does not establish the runtime nondeterminism. Per the playbook this is
"unavailable required artifact", not a negative result.

## Next action that would unblock

Build `coverage_report` and `oracle_gen` from the `v0.108.0` tag on a host with a
Rust toolchain and sufficient memory, then run:

```
driver/scenario_9849.py \
  --coverage-bin <path/to/coverage_report> \
  --oracle-gen-bin <path/to/oracle_gen> \
  --data-dir <v0.108.0 data dir> \
  --out-dir <out> \
  --tool-commit <v0.108.0 tag commit>
```

The scenario asserts byte-identity across two runs of each tool (A2, A4);
per the report both are expected to fail with ordering-only diffs in the tied
top-N structures => `reproduced`.
