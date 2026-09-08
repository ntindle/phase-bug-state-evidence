# phase-bug-state-evidence

Pre- and post-bug authoritative game states captured while reproducing open
[phase-rs/phase](https://github.com/phase-rs/phase) bug issues, oldest to newest.

Each issue gets a folder named by issue number containing:

- `<issue>-<slug>-pre.json` — authoritative game state just before the bug trigger
- `<issue>-<slug>-post.json` — authoritative game state just after the bug trigger
- `<issue>-<slug>.png` — human-readable summary of the repro and verdict

States are exported via the server's `ExportAuthoritativeState` (the same
snapshot the client's "export game state" produces) against the release server
build noted in each issue's comment. They can be re-imported to resume the game
at exactly that point.

Repro driver: scripted WebSocket clients playing scripted decks against the
release `phase-server` in `--single-user` mode.
