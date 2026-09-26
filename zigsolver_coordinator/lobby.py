"""The root of it: which tables the bot host is running, and whether the solver
answers.

Connecting opens the mode=0 socket on the hardcoded indexes table, which is where
the Kotlin side broadcasts

    "Indexes: " + tableRunners.map { it.tableIndex }.joinToString(",")

Every index in that line is a table a front end can open, and the list the bot
switches are kept against (bots.py). Anything else on the socket is a status
line, kept as the lobby's activity. The ZigSolver API is
probed with GET /health at the same time, so an unreachable API is a message on
the root view rather than a surprise mid-hand.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from .answers import now_ms
from .bot_host import HostSocket
from .endpoints import INDEXES_TABLE_INDEX, MODE_HAND
from .preflop import gto_available

if TYPE_CHECKING:
    from .server import Coordinator

ACTIVITY_MAX = 200


class Lobby:
    def __init__(self, co: Coordinator) -> None:
        self.co = co
        self.tables: list[int] = []
        self.activity: list[dict] = []
        # The /health verdict: None, or {state: checking|ok|fail, info?, url?}.
        # FACTS, not the sentence they render as — the front end words them.
        self.health: dict | None = None
        # `/health.solveTuning`: the API's own defaults for the flop knobs, which
        # the settings sheet shows as placeholders.
        self.solve_tuning: dict | None = None
        # Whether the API can answer a preflop spot with the blueprint. False
        # until a /health says otherwise, so nothing offers `gto` for the seconds
        # it takes to find out.
        self.gto_available = False
        self._probe: asyncio.Task | None = None
        self.socket = HostSocket(self._url, self.on_message,
                                 lambda _status: co.state_changed("lobby"), name="lobby")

    def _url(self) -> str | None:
        endpoints = self.co.endpoints
        return endpoints.socket_url(MODE_HAND, INDEXES_TABLE_INDEX) if endpoints.connected else None

    def to_wire(self) -> dict:
        return {"status": self.socket.status, "tables": self.tables, "activity": self.activity,
                "bots": self.co.bots.to_wire()}

    def start(self) -> None:
        """At startup: pick up where the last run left off."""
        if self.co.endpoints.connected:
            self.socket.open()
            self.probe()

    def connect(self) -> None:
        self.tables = []
        self.activity = []
        self.co.state_changed("lobby")
        self.socket.reconnect()
        self.probe()

    def disconnect(self) -> None:
        self.socket.close()
        if self._probe is not None:
            self._probe.cancel()
        self.tables = []
        self.health = None
        self.co.state_changed("lobby", "health")

    def on_message(self, text: str) -> None:
        m = re.search(r"indexes\s*:\s*([\d\s,]*)", text, re.I)
        if m:
            found = (re.match(r"\d+", part.strip()) for part in m.group(1).split(","))
            tables = sorted({int(x.group(0)) for x in found if x})
            if tables != self.tables:
                self.tables = tables
                self.co.state_changed("lobby")
                self.co.bots_listed(tables)
            return
        # Not a table list: a status line from the host.
        self.say(text)

    def say(self, text: str) -> None:
        """A status line for the lobby — notify, then keep it."""
        tone = "warn" if re.search(r"not running|error|fail", text, re.I) else "info"
        self.co.notify(scope="lobby", text=text, tone=tone)
        self.activity = [{"text": text, "at": now_ms()}, *self.activity][:ACTIVITY_MAX]
        self.co.state_changed("lobby")

    def clear_activity(self) -> None:
        self.activity = []
        self.co.state_changed("lobby")

    def probe(self) -> None:
        """GET /health, and say what came back."""
        if self._probe is not None:
            self._probe.cancel()
        url = self.co.endpoints.api_url("/health")
        if not url:
            self.health = None
            self.co.state_changed("health")
            return
        self.health = {"state": "checking"}
        self.co.state_changed("health")
        self._probe = self.co.spawn(self._run_probe(url))

    async def _run_probe(self, url: str) -> None:
        try:
            info = await self.co.api.health()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — unreachable, refused, not JSON: all "fail"
            self.health = {"state": "fail", "url": url}
            self.co.state_changed("health")
            return
        self.health = {"state": "ok", "info": info}
        tuning = info.get("solveTuning")
        self.solve_tuning = tuning if isinstance(tuning, dict) else None
        self.gto_available = gto_available(info)
        self.co.state_changed("health", "solveTuning", "gtoAvailable")
