# ZigSolver Coordinator

The server between the poker bot host, the ZigSolver API and
[ZigSolverFront](../ZigSolverFront). The front end only draws; everything that
turns a table snapshot into an answer happens here:

* the **bot host's sockets** — the lobby's (`Indexes: 0,3,7`) and one per
  watched table — with the AES frames and screenshots sniffed and decrypted;
* the **snapshot**: parsed, the HUD stats and tournament header the operator
  typed written into it in the host's own format, placed in its hand;
* the **question**: the regime (GTO / Exploit / Manual / Advanced) and the
  preflop engine, with the per-hand coins and the manual pick — each table's
  own, so what is picked on one table is not what another plays;
* the **solve pipeline**: a 150ms coalescing window, supersede-and-`/cancel`
  with a unique id per solve, identical and partial re-reads ignored, drifted
  re-reads of the same decision *shielded* (sent beside the running solve
  rather than over it), the stable sample;
* the **answer**: normalized, the play drawn once for every page, the ranges
  collected street by street;
* the **bot switches**: which tables the bot plays is decided here, not on the
  host — per table (`bot`), all running tables at once (`allbot`), and
  auto-enable for tables as they appear (`autoenablebot`). A switch is dropped
  when its table leaves the host's `Indexes:` line, since the host numbers its
  tables from 0 again when it restarts. The switches live in memory only;
* the **bot's move**: while a table's bot is on, the drawn play of each answer
  the hero is to act on goes back down that socket, AES-encrypted like the
  host's own frames, once per decision — `move(fold)`, `move(check)`,
  `move(call)`, `move(bet 33%(4.4 BB))`, `move(raise 60%(27.5 BB))`,
  `move(all-in(41.3 BB))`; an all-in that only calls goes as `move(call)`.
  The host plays every move it gets, so the switch here is the only gate
  (KScanner's README, "The socket");
* the **solve history** per table, and the screen capture behind a failed call;
* **every setting** — endpoints, solve settings, regime, typed values —
  persisted in one place instead of in each browser.

A second tab on the same table, or a second machine, joins the same session and
draws the same felt and the same answer — one socket to the table and one solve
per snapshot, however many pages are watching.

## Running it

```bash
uv run zigsolver-coordinator                  # ws://127.0.0.1:8765
```

or without uv:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/zigsolver-coordinator
```

Then open the front end (`npm run dev` there) — it looks for the coordinator on
its own host at port 8765, and the address can be changed on its connect
screen. Type the bot host and the ZigSolver API there and press Connect; the
coordinator remembers both, and the connection, across restarts.

| option | |
|---|---|
| `--host` | interface to listen on. `127.0.0.1` by default; `0.0.0.0` when the browser is on another machine. |
| `--port` | `8765` by default. |
| `--data-dir` | where the state lives — `~/.zigsolver-coordinator` by default, or `$ZSC_DATA_DIR`. |
| `--origin URL` | accept front ends served from this origin only (repeatable). |
| `--linger SECONDS` | how long a table keeps its socket, its hand and any solve in flight after the last page leaves it (15 by default) — a reload lands inside it. |
| `--log-level` | `debug`, `info` (default), `warning`, `error`. |

A plain `GET /` answers with what is listening, for health checks:
`{"service":"coordinator","version":…,"frontEnds":1,"t":[3]}` — `t` is the
tables with a live session.

Python 3.10 or newer. Three dependencies: `websockets` (both the server for the
front end and the client to the bot host), `httpx` (the ZigSolver API and the
bot host's HTTP) and `cryptography` (the host's AES-256-CBC).

## What is kept, and where

```
~/.zigsolver-coordinator/
    state.json          endpoints (with the API token — written 0600), solve
                        settings, the regime and preflop engine per table,
                        typed stats, typed tournament headers
    history/<table>.json  the last 150 decisions per table
```

Writes are debounced and atomic. Every stored value is re-sanitized on load, so
a hand-edited or older file falls back to defaults rather than restoring
nonsense.

A table nobody has picked a regime or preflop engine on starts from the
defaults — or, on a data directory from before the pickers went per table, from
the one choice all tables shared then, so upgrading changes no table's play
until that table is changed.

The browser keeps only what is about the browser: the coordinator's address,
the theme, the language, which pane of a sheet was open.

## Security

It is a LAN tool, like the hosts on either side of it, but two defaults are
worth knowing:

* it listens on **loopback** unless told otherwise;
* the **API token is never sent to a page** — the front end is told whether one
  is set, and can replace or forget it, but not read it. Only calls at the
  ZigSolver API carry it; nothing at the bot host does.

A WebSocket is not covered by CORS, so any page open in the same browser could
connect to `ws://localhost:8765` and drive the tables. `--origin` closes that:
pass the origin(s) the front end is served from (a client that sends no Origin
at all — a script — is still let in; only a page's socket can be hijacked).

## The protocol

JSON over one WebSocket.

**Down** (coordinator → page):

| type | |
|---|---|
| `hello` | on every connect: `meta` (version, settings defaults/limits/presets, `minStatsForExploit`, …) and the whole global `state` |
| `state` | `patch` — global fields, each named field replaced wholesale: `config`, `lobby` (`status`, `tables`, `activity`, `bots: {enabled, autoEnable}`), `health`, `settings`, `gtoAvailable`, `solveTuning` |
| `table` | `index`, `patch` — one table's fields, to the pages subscribed to it: `socket`, `hand`, `result`, `solving`, `solveElapsed`, `pending`, `regime` (`selected`, `exploitPct`, `requireStats`), `preflop` (`selected`, `gtoPct`), `exploit`, `villainStats`, `drew`, `pfDrew`, `canSolve`, `ranges`, `feed`, `history`, `manual`, `host`, `bot`. The first after a subscribe carries all of them. |
| `notify` | a toast: `text` (the host's own words) or `key`/`params` (one of the front end's message keys), `tone`, `scope` (`table`/`lobby`) |
| `reply` | `id`, `ok`, `data` or `error` — for a request that carried an `id` |

`solveElapsed` is seconds since the solve went out, not a timestamp: the page
may run on a machine whose clock disagrees.

**Up** (page → coordinator), `{type, …}`; add an `id` to get a `reply`:

| | |
|---|---|
| `config.connect` `{host, api, apiToken?}` | store both hosts, connect the lobby, probe `/health` |
| `config.disconnect`, `config.set` `{host?, api?, apiToken?}`, `health.probe` | |
| `lobby.command` `{token}`, `lobby.clearActivity` | `allbot`, `autoenablebot` are handled here; any other token goes to the host |
| `settings.set` `{key, value}`, `settings.reset` | |
| `regime.select` `{index, value}` | that table's regime; re-asks its hand |
| `regime.setExploitPct` `{index, value}`, `regime.setRequireStats` `{index, on}`, `preflop.select` `{index, value}`, `preflop.setGtoPct` `{index, value}` | that table's only |
| `table.subscribe` / `table.unsubscribe` `{index}` | |
| `table.solve`, `table.pick` `{index, regime}`, `table.command` `{index, token}`, `table.clearFeed` | `bot` is handled here; any other token goes to the host's socket for the table |
| `stats.set` `{index, name, key, value}`, `stats.clear`, `tournament.set` `{index, key, value}`, `tournament.clear` | written into the body at once; the felt restates |
| `table.typed` `{index, kind, name?, before}` | an editor closed — re-solves if the values changed since `before` |
| `history.list`, `history.hand` `{key}`, `history.entry` `{entryId}`, `history.clear` | |
| `check.screenshot` `{image, filename, mime, check2, crop, tableIndex}` | relayed to the bot host's `/checkScreenshot` |

## Layout

```
zigsolver_coordinator/
    server.py        the coordinator: state, the protocol, the page connections
    table.py         one table's session — the solve pipeline (port of the
                     front end's TableView)
    lobby.py         the indexes socket, and the /health probe
    hand_body.py     the snapshot parser and writers (port of handBody.js)
    answers.py       /move answers normalized, the play drawn
    ranges.py        the ranges an answer ran on, collected per street
    history.py       every answer, per table, persisted
    regime.py        gto | exploit | manual | advanced, and where exploit applies
    preflop.py       alg | gto | advanced
    per_table.py     those two, kept per table index
    settings.py      how a solve is requested
    manual.py        the typed HUD stats and tournament headers
    endpoints.py     the two hosts, their URLs, the API's token
    bot_host.py      the reconnecting host socket, /image/N, /checkScreenshot
    bot_move.py      an answer's drawn play worded as the bot's `move(...)`
    bots.py          which tables the bot plays
    solver_api.py    /move, /cancel, /health, /screenError
    screen_error.py  the screen behind a failed call
    crypto.py        the host's AES-256-CBC, sniffed
    store.py         the data directory
    jsnum.py         JavaScript's rounding and number printing, where the
                     snapshot's bytes depend on them
    wire.py          JSON that JSON.parse accepts
tests/
    fixtures/        the JS parser's own output for 69 bodies, and the writers'
```

## Tests

```bash
uv run pytest
```

`tests/fixtures/` was generated by running the front end's JavaScript parser —
the code that prepared the hand before this service existed — over the mock
host's snapshots, a seeded fakebot corpus and edge cases. The port is checked
against it byte for byte, so the felt a page draws is the one it drew before.
The rest drive a real `TableSession` against a fake API (supersede, cancel,
shielded re-reads, the manual prompt, the coins, typed stats, captures), and
one test runs a page, the coordinator and a fake bot host over real sockets.

## Not done yet

The macOS shell in ZigSolverFront (`macos/`) starts the solver but not this
service. Until it does, run the coordinator beside the app yourself: the front
end hands the shell's embedded solver endpoint and token to the coordinator on
every connect, so the rest works unchanged.
