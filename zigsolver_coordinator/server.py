"""The coordinator, and the one WebSocket every front end talks to it over.

The front end used to hold two connections per table — the bot host's socket and
the ZigSolver API — and everything between them: parsing, the typed stats, the
regime coins, the solve pipeline, the history, the settings. All of that lives
here now, and a page only displays it. The protocol is small on purpose:

  front end -> coordinator   intents: {type, ...} — `table.subscribe`,
                             `regime.select`, `stats.set`, `table.solve` … A
                             message with an `id` gets a `reply` carrying it.
  coordinator -> front end   `hello` (static meta + the global state), then
                             patches: `state` for the global fields, `table` for
                             one table's; `notify` for a toast; `reply`.

A patch replaces each field it names, wholesale. That makes every field one
function of this process's state (`global_field`, TableSession.wire), so a page
that reconnects, a second page on the same table, and a page that has been open
all day all draw the same thing.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import signal
from http import HTTPStatus
from typing import Any, Callable, Coroutine

import httpx
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import __version__
from .bot_host import check_screenshot
from .bots import Bots
from .endpoints import Endpoints, parse_server
from .hand_body import TOURNAMENT_KEYS
from .history import History
from .lobby import Lobby
from .manual import MANUAL_STAT_KEYS, ManualStats, ManualTournament
from .per_table import PerTable
from .preflop import ENGINES, PreflopState
from .regime import MIN_STATS_FOR_EXPLOIT, REGIMES, RegimeState
from .screen_error import ScreenErrors
from .settings import DEFAULTS, Settings
from .solver_api import SolverApi
from .store import Store
from .table import TableSession
from .wire import dumps

log = logging.getLogger("zsc")

GLOBAL_FIELDS = ["config", "lobby", "health", "settings", "gtoAvailable", "solveTuning"]

MAX_MESSAGE = 64 * 2**20
"""A screenshot for the check view travels in one message."""

MAX_TABLE_INDEX = 10_000_000


class BadRequest(Exception):
    """A message this coordinator will not act on; the reply says why."""


def _index(msg: dict, key: str = "index") -> int:
    v = msg.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_TABLE_INDEX:
        raise BadRequest(f"`{key}` must be a table number")
    return v


def _text(msg: dict, key: str, cap: int = 200) -> str:
    v = msg.get(key)
    if not isinstance(v, str) or not v.strip():
        raise BadRequest(f"`{key}` is required")
    return v[:cap]


def _numbers(values: Any) -> dict:
    """Typed values, compared as numbers: the page sends back `41` for `41.0`."""
    if not isinstance(values, dict):
        return {}
    out = {}
    for k, v in values.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(k)] = float(v)
    return out


class Client:
    """One front end: what it watches, and a queue so its messages go out in order."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.tables: set[int] = set()
        self.closed = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def send(self, text: str) -> None:
        if not self.closed:
            self._queue.put_nowait(text)

    async def pump(self) -> None:
        try:
            while True:
                await self.ws.send(await self._queue.get())
        except ConnectionClosed:
            pass


class Coordinator:
    def __init__(self, store: Store, *, http: httpx.AsyncClient | None = None, linger: float = 15.0) -> None:
        self.store = store
        state = store.read("state.json")
        state = state if isinstance(state, dict) else {}
        self.endpoints = Endpoints(state.get("config"))
        self.settings = Settings(state.get("settings"))
        # Per table: what is picked on one table's bar is that table's alone.
        self.regime = PerTable(RegimeState, state.get("regime"))
        self.preflop = PerTable(PreflopState, state.get("preflop"))
        self.manual_stats = ManualStats(state.get("manualStats"))
        self.manual_tournament = ManualTournament(state.get("manualTournament"))
        self.history = History(store)
        self.bots = Bots()

        # Direct connections: the hosts are on the operator's own network, and a
        # proxy picked up from the environment is how a LAN address stops working.
        self.http = http or httpx.AsyncClient(trust_env=False)
        self.api = SolverApi(self.http, self.endpoints, self.spawn)
        self.screen_errors = ScreenErrors(self.http, self.endpoints, self.api)
        self.lobby = Lobby(self)

        self.tables: dict[int, TableSession] = {}
        self.clients: set[Client] = set()
        # How long a table outlives its last front end. A reload, or a page
        # swapping one table for another and back, lands inside it and finds the
        # hand, the answer and the solve in flight still there.
        self.linger = linger

        self._tasks: set[asyncio.Task] = set()
        self._dirty_global: set[str] = set()
        self._dirty_tables: dict[int, set[str]] = {}
        self._flush_handle: asyncio.Handle | None = None

        self._handlers: dict[str, Callable[[Client, dict], Any]] = {
            "config.connect": self._config_connect,
            "config.disconnect": self._config_disconnect,
            "config.set": self._config_set,
            "health.probe": lambda c, m: self.lobby.probe(),
            "lobby.command": self._lobby_command,
            "lobby.clearActivity": lambda c, m: self.lobby.clear_activity(),
            "settings.set": self._settings_set,
            "settings.reset": self._settings_reset,
            "regime.select": self._regime_select,
            "regime.setExploitPct": lambda c, m: self._set_mode(m, "regime", lambda r: r.set_exploit_pct(m.get("value"))),
            "regime.setRequireStats": lambda c, m: self._set_mode(m, "regime", lambda r: r.set_require_stats(m.get("on"))),
            "preflop.select": self._preflop_select,
            "preflop.setGtoPct": lambda c, m: self._set_mode(m, "preflop", lambda p: p.set_gto_pct(m.get("value"))),
            "table.subscribe": lambda c, m: self.subscribe(c, _index(m)),
            "table.unsubscribe": lambda c, m: self.unsubscribe(c, _index(m)),
            "table.solve": self._table_solve,
            "table.pick": self._table_pick,
            "table.command": self._table_command,
            "table.clearFeed": lambda c, m: self._with_session(m, lambda s: s.clear_feed()),
            "table.typed": self._table_typed,
            "stats.set": self._stats_set,
            "stats.clear": self._stats_clear,
            "tournament.set": self._tournament_set,
            "tournament.clear": self._tournament_clear,
            "history.list": lambda c, m: self.history.list_wire(_index(m)),
            "history.hand": lambda c, m: self.history.hand_wire(_index(m), m.get("key")),
            # `entryId`, not `id`: `id` is the request's own, the one its reply carries.
            "history.entry": lambda c, m: self.history.entry_wire(_index(m), m.get("entryId")),
            "history.clear": self._history_clear,
            "check.screenshot": self._check_screenshot,
        }
        # Handlers that wait on a host run beside the client's other messages
        # rather than in front of them.
        self._slow = {"check.screenshot"}

    # --- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        self.lobby.start()

    async def stop(self) -> None:
        for session in self.tables.values():
            session.close()
        self.tables.clear()
        self.lobby.socket.close()
        for task in list(self._tasks):
            task.cancel()
        self.store.flush()
        await self.http.aclose()

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """A background task, kept referenced until it ends, its failure logged."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    # --- persistence ------------------------------------------------------------------

    def save(self) -> None:
        self.store.write_soon("state.json", self._state_for_store, private=True)

    def _state_for_store(self) -> dict:
        return {
            "config": self.endpoints.to_store(),
            "settings": self.settings.to_wire(),
            "regime": self.regime.to_store(),
            "preflop": self.preflop.to_store(),
            "manualStats": self.manual_stats.to_store(),
            "manualTournament": self.manual_tournament.to_store(),
        }

    def _saved(self, changed: bool, field: str) -> None:
        if changed:
            self.save()
            self.state_changed(field)

    # --- what the front ends are told ---------------------------------------------------

    def meta(self) -> dict:
        """What does not change while this process runs."""
        return {
            "version": __version__,
            "settings": Settings.meta(),
            "minStatsForExploit": MIN_STATS_FOR_EXPLOIT,
            "manualStatKeys": MANUAL_STAT_KEYS,
            "tournamentKeys": TOURNAMENT_KEYS,
        }

    def global_field(self, name: str) -> Any:
        if name == "config":
            return self.endpoints.to_wire()
        if name == "lobby":
            return self.lobby.to_wire()
        if name == "health":
            return self.lobby.health
        if name == "settings":
            return self.settings.to_wire()
        if name == "gtoAvailable":
            return self.lobby.gto_available
        if name == "solveTuning":
            return self.lobby.solve_tuning
        raise KeyError(name)

    def state_changed(self, *fields: str) -> None:
        self._dirty_global.update(fields)
        self._schedule_flush()

    def table_changed(self, session: TableSession, fields) -> None:
        self._dirty_tables.setdefault(session.index, set()).update(fields)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        # Once per loop turn: a snapshot that moves six fields is one message.
        if self._flush_handle is None:
            try:
                self._flush_handle = asyncio.get_running_loop().call_soon(self.flush)
            except RuntimeError:
                pass  # no loop (a unit test): whoever made the change flushes

    def flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._dirty_global:
            fields, self._dirty_global = self._dirty_global, set()
            patch = {f: self.global_field(f) for f in GLOBAL_FIELDS if f in fields}
            self._broadcast({"type": "state", "patch": patch}, self.clients)
        if self._dirty_tables:
            dirty, self._dirty_tables = self._dirty_tables, {}
            for index, fields in dirty.items():
                session = self.tables.get(index)
                if session is not None and session.subscribers:
                    self._broadcast({"type": "table", "index": index, "patch": session.patch(fields)},
                                    session.subscribers)

    def notify(self, *, scope: str, index: int | None = None, text: str | None = None,
               key: str | None = None, params: dict | None = None, tone: str = "info") -> None:
        """A toast. Either the host's own words (`text`), or one of ours as a
        message key the front end words in its own language."""
        self.flush()
        message: dict[str, Any] = {"type": "notify", "scope": scope, "tone": tone}
        if index is not None:
            message["index"] = index
        if text is not None:
            message["text"] = text
        if key:
            message["key"] = key
            if params:
                message["params"] = params
        if scope == "table":
            session = self.tables.get(index) if index is not None else None
            targets = session.subscribers if session else set()
        else:
            targets = self.clients
        self._broadcast(message, targets)

    @staticmethod
    def _broadcast(message: dict, clients) -> None:
        if not clients:
            return
        text = dumps(message)
        for client in list(clients):
            client.send(text)

    # --- connections -----------------------------------------------------------------

    def http_request(self, connection: ServerConnection, request) -> Any:
        """A plain GET instead of a WebSocket: say what this is, for curl and for
        whatever checks that the port is up."""
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        body = dumps({"service": "coordinator", "version": __version__,
                      "frontEnds": len(self.clients), "t": sorted(self.tables)})
        response = connection.respond(HTTPStatus.OK, body + "\n")
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "application/json"
        return response

    async def handle(self, ws: ServerConnection) -> None:
        client = Client(ws)
        self.clients.add(client)
        pump = asyncio.create_task(client.pump())
        log.info("front end connected (%s)", ws.remote_address[0] if ws.remote_address else "?")
        client.send(dumps({"type": "hello", "meta": self.meta(),
                           "state": {f: self.global_field(f) for f in GLOBAL_FIELDS}}))
        try:
            async for raw in ws:
                self.dispatch(client, raw)
        except ConnectionClosed:
            pass
        finally:
            client.closed = True
            self.clients.discard(client)
            for index in list(client.tables):
                self.unsubscribe(client, index)
            pump.cancel()
            log.info("front end disconnected")

    def dispatch(self, client: Client, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        rid = msg.get("id")
        kind = msg.get("type")
        handler = self._handlers.get(kind) if isinstance(kind, str) else None
        if handler is None:
            self._reply(client, rid, error=f"unknown message type {kind!r}")
            return
        if kind in self._slow:
            self.spawn(self._run_slow(client, rid, kind, handler(client, msg)))
            return
        try:
            data = handler(client, msg)
        except BadRequest as exc:
            self._reply(client, rid, error=str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("%s failed", kind)
            self._reply(client, rid, error="the coordinator failed to handle this")
            return
        self._reply(client, rid, data=data)

    async def _run_slow(self, client: Client, rid: Any, kind: str, work: Coroutine) -> None:
        try:
            data = await work
        except BadRequest as exc:
            self._reply(client, rid, error=str(exc))
        except httpx.HTTPError as exc:
            self._reply(client, rid, error=f"the bot host did not answer: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("%s failed", kind)
            self._reply(client, rid, error="the coordinator failed to handle this")
        else:
            self._reply(client, rid, data=data)

    def _reply(self, client: Client, rid: Any, *, data: Any = None, error: str | None = None) -> None:
        if rid is None:
            return
        # Whatever the request changed goes out first, so a page that awaits the
        # reply finds its state already moved.
        self.flush()
        reply: dict[str, Any] = {"type": "reply", "id": rid, "ok": error is None}
        if error is None:
            reply["data"] = data
        else:
            reply["error"] = error
        client.send(dumps(reply))

    # --- tables -------------------------------------------------------------------------

    def subscribe(self, client: Client, index: int) -> None:
        session = self.tables.get(index)
        if session is None:
            session = TableSession(self, index)
            self.tables[index] = session
            session.start()
            log.info("table %s: session started", index)
        if session.linger is not None:
            session.linger.cancel()
            session.linger = None
        # Pending patches go to the pages that already have the table; this one
        # gets the whole of it, fresh.
        self.flush()
        session.subscribers.add(client)
        client.tables.add(index)
        client.send(dumps({"type": "table", "index": index, "patch": session.full()}))

    def unsubscribe(self, client: Client, index: int) -> None:
        client.tables.discard(index)
        session = self.tables.get(index)
        if session is None:
            return
        session.subscribers.discard(client)
        if not session.subscribers and session.linger is None:
            session.linger = asyncio.get_running_loop().call_later(self.linger, self._expire, index)

    def _expire(self, index: int) -> None:
        session = self.tables.get(index)
        if session is None or session.subscribers:
            return
        session.close()
        del self.tables[index]
        self._dirty_tables.pop(index, None)
        log.info("table %s: session closed, nobody is watching", index)

    def _with_session(self, msg: dict, act: Callable[[TableSession], Any]) -> Any:
        session = self.tables.get(_index(msg))
        return act(session) if session is not None else None

    # --- the handlers ----------------------------------------------------------------

    def _endpoints_changed(self, changed: set[str], *, lobby_done: bool = False) -> None:
        if "host" in changed:
            if not lobby_done and self.endpoints.connected:
                self.lobby.socket.reconnect()
            for session in self.tables.values():
                session.socket.reconnect()
        if changed & {"api", "apiToken"}:
            if not lobby_done and self.endpoints.connected:
                self.lobby.probe()
            for session in self.tables.values():
                session.changed("canSolve")

    def _config_connect(self, client: Client, msg: dict) -> None:
        if parse_server(msg.get("host")) is None:
            raise BadRequest("the bot host is not an address")
        if parse_server(msg.get("api")) is None:
            raise BadRequest("the API is not an address")
        token = msg.get("apiToken")
        changed = self.endpoints.update(host=msg["host"], api=msg["api"],
                                        api_token=token if isinstance(token, str) else None)
        self.endpoints.connected = True
        self.save()
        self.state_changed("config")
        self.lobby.connect()
        self._endpoints_changed(changed, lobby_done=True)

    def _config_disconnect(self, client: Client, msg: dict) -> None:
        self.endpoints.connected = False
        self.save()
        self.state_changed("config")
        self.lobby.disconnect()

    def _config_set(self, client: Client, msg: dict) -> None:
        values = {arg: msg[key] for key, arg in (("host", "host"), ("api", "api"), ("apiToken", "api_token"))
                  if isinstance(msg.get(key), str)}
        changed = self.endpoints.update(**values)
        if changed:
            self.save()
            self.state_changed("config")
            self._endpoints_changed(changed)

    def _settings_set(self, client: Client, msg: dict) -> None:
        key = msg.get("key")
        if key not in DEFAULTS:
            raise BadRequest(f"no setting {key!r}")
        self._saved(self.settings.set(key, msg.get("value")), "settings")

    def _settings_reset(self, client: Client, msg: dict) -> None:
        self._saved(self.settings.reset(), "settings")

    def _set_mode(self, msg: dict, field: str, change: Callable[[Any], bool]) -> None:
        """Change one table's regime or preflop engine (`field`): kept, and
        drawn on the pages watching that table — no other table's changed."""
        modes = self.regime if field == "regime" else self.preflop
        if change(modes.of(_index(msg))):
            self.save()
            self._with_session(msg, lambda s: s.changed(field))

    def _regime_select(self, client: Client, msg: dict) -> None:
        value = msg.get("value")
        if value not in REGIMES:
            raise BadRequest(f"no regime {value!r}")
        self._set_mode(msg, "regime", lambda r: r.select(value))
        # Picking a regime is a gesture about the hand on THAT felt, and it
        # re-asks it.
        self._with_session(msg, lambda s: s.on_regime_selected(value))

    def _preflop_select(self, client: Client, msg: dict) -> None:
        value = msg.get("value")
        if value not in ENGINES:
            raise BadRequest(f"no preflop engine {value!r}")
        self._set_mode(msg, "preflop", lambda p: p.select(value))

    def _table_solve(self, client: Client, msg: dict) -> None:
        self._with_session(msg, lambda s: s.solve())

    def _table_pick(self, client: Client, msg: dict) -> None:
        regime = msg.get("regime")
        if regime not in ("gto", "exploit"):
            raise BadRequest("the answer to the manual prompt is gto or exploit")
        self._with_session(msg, lambda s: s.solve(regime))

    def _lobby_command(self, client: Client, msg: dict) -> dict:
        token = _text(msg, "token", 100)
        if token == "allbot":
            self.toggle_all_bots()
            return {"ok": True}
        if token == "autoenablebot":
            self.lobby.say(f"Auto enable bots: {str(self.bots.toggle_auto_enable()).lower()}")
            return {"ok": True}
        return {"ok": self.lobby.socket.send(token)}

    def _table_command(self, client: Client, msg: dict) -> dict:
        token = _text(msg, "token", 100)
        index = _index(msg)
        if token == "bot":
            self.set_bot(index, not self.bots.is_on(index))
            return {"ok": True}
        session = self.tables.get(index)
        return {"ok": bool(session) and session.socket.send(token)}

    # --- the bot switches -----------------------------------------------------------

    def set_bot(self, index: int, on: bool) -> None:
        if self.bots.set(index, on):
            self._bot_switched(index, on)

    def toggle_all_bots(self) -> None:
        """Every running table on — or, when every one already is, every one off."""
        tables = self.lobby.tables
        on = not (tables and all(self.bots.is_on(i) for i in tables))
        for index in tables:
            self.set_bot(index, on)
        self.lobby.say("All bots enabled" if on else "All bots disabled")

    def bots_listed(self, tables: list[int]) -> None:
        """The host's table list moved: the switches follow it (Bots.listed)."""
        on, off = self.bots.listed(tables)
        for index in on:
            self._bot_switched(index, True)
        for index in off:
            self._bot_switched(index, False)

    def _bot_switched(self, index: int, on: bool) -> None:
        log.info("table %s: bot %s", index, "enabled" if on else "disabled")
        self.state_changed("lobby")
        session = self.tables.get(index)
        if session is not None:
            session.bot_switched(on)

    def _table_typed(self, client: Client, msg: dict) -> None:
        """An editor closed. `before` is what the page saw when it opened, so the
        comparison runs AFTER every keystroke it sent has landed — the page cannot
        make it itself, since the last keystroke may still be on its way here."""
        index = _index(msg)
        kind = msg.get("kind")
        if kind == "stats":
            current = self.manual_stats.table(index).get(_text(msg, "name")) or {}
        elif kind == "tournament":
            current = self.manual_tournament.values(index) or {}
        else:
            raise BadRequest("`kind` is stats or tournament")
        changed = _numbers(msg.get("before")) != _numbers(current)
        self._with_session(msg, lambda s: s.resolve_typed(changed))

    def _typed(self, index: int) -> None:
        self.save()
        session = self.tables.get(index)
        if session is not None:
            session.restate()
            session.changed("manual")

    def _stats_set(self, client: Client, msg: dict) -> None:
        index = _index(msg)
        key = msg.get("key")
        if key not in MANUAL_STAT_KEYS:
            raise BadRequest(f"{key!r} cannot be typed")
        if self.manual_stats.set(index, _text(msg, "name"), key, msg.get("value")):
            self._typed(index)

    def _stats_clear(self, client: Client, msg: dict) -> None:
        index = _index(msg)
        if self.manual_stats.clear(index, _text(msg, "name")):
            self._typed(index)

    def _tournament_set(self, client: Client, msg: dict) -> None:
        index = _index(msg)
        key = msg.get("key")
        if key not in TOURNAMENT_KEYS:
            raise BadRequest(f"{key!r} is not a header field")
        if self.manual_tournament.set(index, key, msg.get("value")):
            self._typed(index)

    def _tournament_clear(self, client: Client, msg: dict) -> None:
        index = _index(msg)
        if self.manual_tournament.clear(index):
            self._typed(index)

    def _history_clear(self, client: Client, msg: dict) -> None:
        index = _index(msg)
        self.history.clear(index)
        self._with_session(msg, lambda s: s.changed("history"))

    async def _check_screenshot(self, client: Client, msg: dict) -> dict:
        try:
            image = base64.b64decode(msg.get("image") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise BadRequest("the image is not base64") from None
        if not image:
            raise BadRequest("no image")
        table_index = msg.get("tableIndex")
        out = await check_screenshot(
            self.http, self.endpoints, image=image,
            filename=str(msg.get("filename") or "screenshot.png")[:200],
            mime=str(msg.get("mime") or "")[:100],
            check2=bool(msg.get("check2")),
            crop=str(msg.get("crop") or "").strip()[:64],
            table_index="" if table_index is None else str(table_index).strip()[:12],
        )
        body = out.pop("body")
        if out["ok"] and out["contentType"].startswith("image/"):
            out["image"] = base64.b64encode(body).decode("ascii")
        else:
            out["text"] = body[:1_000_000].decode("utf-8", errors="replace")
        return out


async def run(*, host: str, port: int, data_dir: str, origins: list[str] | None = None,
              linger: float = 15.0, ready: Callable[[int], None] | None = None,
              stop: asyncio.Event | None = None) -> None:
    """Serve until SIGINT/SIGTERM (or `stop`)."""
    co = Coordinator(Store(data_dir), linger=linger)
    await co.start()
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    # A browser always sends an Origin; only a page's socket can be hijacked,
    # so a client that sends none (a script, a test) is let through either way.
    allowed = [*origins, None] if origins else None
    try:
        async with serve(co.handle, host, port, origins=allowed, process_request=co.http_request,
                         max_size=MAX_MESSAGE) as server:
            bound = server.sockets[0].getsockname()[1] if server.sockets else port
            log.info("listening on ws://%s:%s — data in %s", host, bound, co.store.dir)
            if ready:
                ready(bound)
            await stop.wait()
    finally:
        await co.stop()
