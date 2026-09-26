"""Shared fixtures: a programmable fake API, and a coordinator wired to it.

The fake answers over httpx.MockTransport, so a test drives the real SolverApi,
the real TableSession and the real history — only the network is pretend. It
also serves the bot host's `/image/N` (the paths do not overlap), which is what
the screen capture fetches.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from zigsolver_coordinator.server import Coordinator
from zigsolver_coordinator.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
BODIES = {c["name"].removeprefix("mock-"): c["body"]
          for c in json.loads((FIXTURES / "hand_body_parse.json").read_text())
          if c["name"].startswith("mock-")}

# A 3-way flop, the hero to act — exploit refuses it (multiway).
FLOP_3WAY = BODIES["SNAPSHOT"]
# Heads-up since the flop, on the turn — the one shape exploit answers.
TURN_HU = BODIES["SNAPSHOT_HU_FLOP"]
# The same hand, one street on.
RIVER_HU = (
    TURN_HU.replace("Total pot 12.4BB", "Total pot 16.4BB")
    .replace(
        "*me*\nposition=CO\nwaiting\nhand=A♦J♦\nstack=55.6BB\n",
        "*me*\nposition=CO\ncheck\nhand=A♦J♦\nstack=55.6BB\n\nBoard: K♠ 8♦ 3♦ 7♣ 2♥\n\n"
        "Big Stack Bob\nposition=BB\nbet 4BB\nstack=51.6BB\n\n"
        "*me*\nposition=CO\nwaiting\nhand=A♦J♦\nstack=55.6BB\n",
    )
)
# Heads-up preflop.
PREFLOP_HU = BODIES["SNAPSHOT_HU"]
# A 9-max flop — a different hand from FLOP_3WAY.
FLOP_9MAX = BODIES["SNAPSHOT_9MAX"]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def answer(payload: dict, actions=None, *, street="flop", ranges=True) -> dict:
    """A /move answer shaped like the real endpoint's."""
    regime = payload.get("regime", "gto")
    meta = {"flow": "exact", "warnings": []}
    if ranges and regime == "gto":
        meta["ranges"] = {"board": ["4h", "Td", "8c"], "players": [
            {"name": "*me*", "hero": True, "combos": 100, "widthPct": 12.5, "weights": {"AA": 1, "QJs": 1}},
            {"name": "Dmitri O", "combos": 200, "widthPct": 25, "weights": {"KK": 0.5}},
        ]}
    return {
        "regime": regime, "regimeRequested": regime, "solver": "exact", "street": street,
        "hand": "QsJs", "responseTime": 0.05,
        "actions": actions or [{"action": "check", "probability": 0.4},
                               {"action": "bet 33%(4.4BB)", "probability": 0.6}],
        "meta": meta,
    }


class FakeApi:
    """The ZigSolver API (and the bot host's /image/N), recording every call."""

    def __init__(self) -> None:
        self.moves: list[dict] = []
        self.cancels: list[str] = []
        self.screen_errors: list[dict] = []
        self.images: list[str] = []
        self.delay = 0.05
        # payload -> (status, body) or (status, body, delay); None = a GTO answer
        self.respond = None
        self.unreachable = False
        self.health = {"status": "ok", "preflopEngines": ["alg", "gto"],
                       "solveTuning": {"gateExploitability": {"default": 3.0}}}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/move":
            payload = json.loads(request.content)
            self.moves.append(payload)
            if self.unreachable:
                raise httpx.ConnectError("connection refused", request=request)
            out = self.respond(payload) if self.respond else (200, answer(payload))
            status, body = out[0], out[1]
            await asyncio.sleep(out[2] if len(out) > 2 else self.delay)
            return httpx.Response(status, json=body)
        if path == "/cancel":
            self.cancels.append(json.loads(request.content)["requestId"])
            return httpx.Response(200, json={"found": True})
        if path == "/health":
            return httpx.Response(200, json=self.health)
        if path == "/screenError":
            self.screen_errors.append(json.loads(request.content))
            return httpx.Response(200, json={"stored": True, "file": "hand.png"})
        if path.startswith("/image/"):
            self.images.append(path)
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def make(tmp_path, api):
    """make() -> a Coordinator whose endpoints point at the fake."""

    def build(*, linger: float = 0.05, data_dir=None) -> Coordinator:
        http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
        co = Coordinator(Store(data_dir or tmp_path), http=http, linger=linger)
        co.endpoints.update(host="bot.test:8080", api="api.test:8000")
        return co

    return build


def run(coro):
    return asyncio.run(coro)


async def settle(seconds: float = 0.25) -> None:
    """Long enough for the coalescing window and a fake solve to pass."""
    await asyncio.sleep(seconds)
