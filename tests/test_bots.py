"""The bot switches: the coordinator's own, kept against the host's table list."""
from __future__ import annotations

from conftest import run
from zigsolver_coordinator.bots import Bots


def test_a_table_the_host_stops_running_loses_its_switch():
    bots = Bots()
    assert bots.listed([0, 1]) == ([], [])
    bots.set(1, True)
    # The host restarted and numbers its tables from 0 again: index 1 is not
    # the table that was switched on.
    assert bots.listed([0]) == ([], [1])
    assert bots.listed([0, 1]) == ([], [])
    assert not bots.is_on(1)


def test_auto_enable_switches_on_the_tables_that_appear():
    bots = Bots()
    bots.listed([0])
    assert bots.toggle_auto_enable()
    # Only what is new: a table already running when it was turned on stays off.
    assert bots.listed([0, 2]) == ([2], [])
    assert bots.enabled == {2}
    bots.set(2, False)
    assert bots.listed([0, 2, 3]) == ([3], [])
    assert bots.enabled == {3}


def test_lobby_commands_are_handled_here(make):
    async def go():
        co = make()
        forwarded: list[str] = []
        co.lobby.socket.send = lambda text: forwarded.append(text) or True
        co.lobby.on_message("Indexes: 0,3,7")

        command = lambda token: co._lobby_command(None, {"token": token})  # noqa: E731
        assert command("allbot") == {"ok": True}
        assert co.bots.enabled == {0, 3, 7}
        co.set_bot(3, False)
        # Not every one is on, so the toggle turns every one on.
        command("allbot")
        assert co.bots.enabled == {0, 3, 7}
        command("allbot")
        assert co.bots.enabled == set()
        assert [a["text"] for a in co.lobby.activity][:3] == [
            "All bots disabled", "All bots enabled", "All bots enabled"]

        command("autoenablebot")
        assert co.bots.auto_enable and co.lobby.activity[0]["text"] == "Auto enable bots: true"
        co.lobby.on_message("Indexes: 0,3,7,8")
        assert co.bots.enabled == {8}
        assert co.lobby.to_wire()["bots"] == {"enabled": [8], "autoEnable": True}

        # Nothing about the bot reaches the host; anything else still does.
        assert forwarded == []
        command("something")
        assert forwarded == ["something"]
        await co.http.aclose()
    run(go())
