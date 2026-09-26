"""Which tables the bot plays.

The coordinator's own switches, and nobody else's: the bot host executes every
`move(...)` it is sent, so a table whose bot is off is simply a table no move
goes out for (TableSession._send_bot_move).

A switch belongs to a table the host is RUNNING. The host numbers its tables
from 0 each time it starts, so a switch left on for an index that has gone from
the lobby's `Indexes:` line would turn the bot on for whatever table is given
that number next — it is dropped instead. With auto-enable on, a table that
appears in the line is switched on as it arrives.

Kept in memory only, like the host kept them: after a restart every bot is off
until someone turns it on.
"""
from __future__ import annotations


class Bots:
    def __init__(self) -> None:
        self.enabled: set[int] = set()
        self.auto_enable = False
        # The indexes in the last `Indexes:` line, or None before the first.
        self._listed: set[int] | None = None

    def to_wire(self) -> dict:
        return {"enabled": sorted(self.enabled), "autoEnable": self.auto_enable}

    def is_on(self, index: int) -> bool:
        return index in self.enabled

    def set(self, index: int, on: bool) -> bool:
        """Switch one table; True when that changed anything."""
        if on == self.is_on(index):
            return False
        if on:
            self.enabled.add(index)
        else:
            self.enabled.discard(index)
        return True

    def toggle_auto_enable(self) -> bool:
        self.auto_enable = not self.auto_enable
        return self.auto_enable

    def listed(self, tables: list[int]) -> tuple[list[int], list[int]]:
        """The host's table list has changed -> (switched on, switched off).

        Off: every table that is no longer running. On: with auto-enable, every
        table that was not in the last list — which is every table on the first
        list, since until then nothing is known to have been running."""
        now = set(tables)
        before = self._listed if self._listed is not None else set()
        self._listed = now
        off = sorted(i for i in self.enabled if i not in now)
        self.enabled.difference_update(off)
        on = sorted(i for i in now - before if self.set(i, True)) if self.auto_enable else []
        return on, off
