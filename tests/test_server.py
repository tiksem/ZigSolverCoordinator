"""End to end: a front end on the coordinator's socket, a fake bot host on the
other side, the fake API behind it."""
from __future__ import annotations

import asyncio
import json

import httpx
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from conftest import FLOP_3WAY, run
from zigsolver_coordinator.crypto import decrypt_message
from zigsolver_coordinator.endpoints import INDEXES_TABLE_INDEX
from zigsolver_coordinator.server import Coordinator
from zigsolver_coordinator.store import Store


class FakeBotHost:
    """The Kotlin runner: the index broadcast, one table, and the commands it gets."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.port = 0

    async def handler(self, ws) -> None:
        index = int(ws.request.path.split("tableIndex=")[1])
        if index == INDEXES_TABLE_INDEX:
            await ws.send("Indexes: 3,7")
        else:
            await ws.send("New hand #1")
            await ws.send(FLOP_3WAY)
        async for message in ws:
            self.commands.append(message)
            if message == "read":
                await ws.send(FLOP_3WAY)


class Front:
    """A front end: sends intents, collects whatever the coordinator pushes."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.state: dict = {}
        self.tables: dict[int, dict] = {}
        self.toasts: list[dict] = []
        self.replies: dict[int, dict] = {}
        self._seq = 0
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg["type"] == "hello":
                self.meta = msg["meta"]
                self.state.update(msg["state"])
            elif msg["type"] == "state":
                self.state.update(msg["patch"])
            elif msg["type"] == "table":
                self.tables.setdefault(msg["index"], {}).update(msg["patch"])
            elif msg["type"] == "notify":
                self.toasts.append(msg)
            elif msg["type"] == "reply":
                self.replies[msg["id"]] = msg

    async def ask(self, type_: str, **data) -> dict:
        self._seq += 1
        rid = self._seq
        await self.ws.send(json.dumps({"type": type_, "id": rid, **data}))
        for _ in range(200):
            if rid in self.replies:
                return self.replies.pop(rid)
            await asyncio.sleep(0.02)
        raise AssertionError(f"no reply to {type_}")

    async def until(self, check, timeout: float = 5.0) -> None:
        for _ in range(int(timeout / 0.02)):
            if check():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("timed out waiting")


def test_a_front_end_drives_a_table_over_the_socket(tmp_path, api):
    async def go():
        bot = FakeBotHost()
        async with serve(bot.handler, "127.0.0.1", 0) as bot_server:
            bot_port = bot_server.sockets[0].getsockname()[1]
            http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
            co = Coordinator(Store(tmp_path), http=http, linger=0.2)
            async with serve(co.handle, "127.0.0.1", 0, process_request=co.http_request) as server:
                port = server.sockets[0].getsockname()[1]

                # A plain GET says what is listening.
                async with httpx.AsyncClient() as plain:
                    status = (await plain.get(f"http://127.0.0.1:{port}/")).json()
                assert status["service"] == "coordinator" and status["t"] == []

                async with connect(f"ws://127.0.0.1:{port}/") as ws:
                    front = Front(ws)
                    await front.until(lambda: "config" in front.state)
                    assert front.meta["minStatsForExploit"] == 3

                    bad = await front.ask("config.connect", host="", api="api.test")
                    assert not bad["ok"] and "bot host" in bad["error"]

                    ok = await front.ask("config.connect", host=f"127.0.0.1:{bot_port}",
                                         api="api.test", apiToken="tok")
                    assert ok["ok"]
                    assert front.state["config"]["apiTokenSet"] and "tok" not in json.dumps(front.state)
                    await front.until(lambda: front.state["lobby"]["tables"] == [3, 7])
                    await front.until(lambda: (front.state.get("health") or {}).get("state") == "ok")
                    assert front.state["gtoAvailable"] is True

                    await front.ask("table.subscribe", index=3)
                    await front.until(lambda: (front.tables.get(3, {}).get("result") or {}).get("type") == "answer")
                    table = front.tables[3]
                    assert table["hand"]["heroHand"] == ["Qs", "Js"]
                    assert table["ranges"][0]["street"] == "flop"
                    assert table["history"]["count"] == 1
                    assert [t["text"] for t in front.toasts if t.get("index") == 3] == ["New hand #1"]
                    # The token went to the API, and only there.
                    assert api.moves and api.moves[0]["body"] == FLOP_3WAY

                    # The bot switch is the coordinator's own: it goes nowhere near the host.
                    sent = await front.ask("table.command", index=3, token="bot")
                    assert sent["data"] == {"ok": True}
                    assert front.tables[3]["bot"] is True
                    assert front.state["lobby"]["bots"]["enabled"] == [3]
                    await front.until(lambda: "Bot enabled" in [t.get("text") for t in front.toasts
                                                                  if t.get("index") == 3])
                    # Switched on with the answer up: that answer is played, and the
                    # host is sent the move and nothing else.
                    await front.until(lambda: len(bot.commands) == 1)
                    assert decrypt_message(bot.commands[0]).startswith("move(")
                    await front.ask("table.command", index=3, token="pause")
                    await front.until(lambda: bot.commands[1:] == ["pause"])

                    await front.ask("stats.set", index=3, name="Sasha M", key="VPIP", value=41)
                    assert front.tables[3]["manual"]["stats"]["Sasha M"]["lines"] == ["VPIP=41%"]
                    assert "VPIP=41%" in front.tables[3]["hand"]["raw"]
                    await front.ask("table.typed", index=3, kind="stats", name="Sasha M", before={})
                    await front.until(lambda: len(api.moves) == 2)
                    assert api.moves[1]["handId"].endswith("s1")

                    listed = await front.ask("history.list", index=3)
                    assert listed["data"]["decisions"] >= 1
                    hand = listed["data"]["hands"][0]
                    full = await front.ask("history.hand", index=3, key=hand["key"])
                    assert full["data"]["record"]["heroHand"] == ["Qs", "Js"]
                    first = hand["entries"][0]["id"]
                    entry = await front.ask("history.entry", index=3, entryId=first)
                    assert entry["data"]["id"] == first and entry["data"]["hand"]["streetName"] == "flop"

                    set_bad = await front.ask("settings.set", key="nope", value=1)
                    assert not set_bad["ok"]
                    await front.ask("settings.set", key="maxSolveTime", value=30)
                    assert front.state["settings"]["maxSolveTime"] == 30

                    # The pickers are the table's own: set on 3, they are 3's alone.
                    assert "regime" not in front.state and "preflop" not in front.state
                    assert front.tables[3]["regime"]["selected"] == "gto"
                    no_table = await front.ask("regime.select", value="exploit")
                    assert not no_table["ok"] and "index" in no_table["error"]
                    await front.ask("regime.select", index=3, value="exploit")
                    await front.ask("preflop.setGtoPct", index=3, value=80)
                    assert front.tables[3]["regime"]["selected"] == "exploit"
                    assert front.tables[3]["preflop"]["gtoPct"] == 80
                    await front.ask("table.subscribe", index=7)
                    assert front.tables[7]["regime"]["selected"] == "gto"
                    assert front.tables[7]["preflop"]["gtoPct"] == 50

                    await front.ask("table.unsubscribe", index=3)
                    assert 3 in co.tables              # lingers...
                    await asyncio.sleep(0.4)
                    assert 3 not in co.tables          # ...then goes
            await co.stop()

        # Everything the operator set survived in the data directory.
        again = Coordinator(Store(tmp_path))
        assert again.settings["maxSolveTime"] == 30
        assert again.endpoints.api_token == "tok" and again.endpoints.connected
        assert again.manual_stats.table(3) == {"Sasha M": {"VPIP": 41}}
        assert again.regime.of(3).selected == "exploit" and again.preflop.of(3).gto_pct == 80
        assert again.regime.of(7).selected == "gto" and again.preflop.of(7).gto_pct == 50
        assert again.history.count(3) >= 1
        await again.http.aclose()

    run(go())
