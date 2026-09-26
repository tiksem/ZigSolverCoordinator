"""What the operator types that the snapshot does not carry.

**The four HUD stats.** The host writes whatever its HUD carries, and it often
carries nothing: a table that just opened, a client that anonymises its seats, a
villain the tracker has never seen. Every stat the body does not say is imputed
from the population on the solver side — so the exploit models describe the
average player rather than this one, and the preflop chart's widths are bent by
numbers nobody measured here. When the operator knows better than the HUD, this
is where they say so.

**The tournament header.** Players left, players paid, average stack — the
difference between an answer in chips and an answer in money. The endpoint
prices a hand under ICM only when the body says how many are left AND how many
places pay, and can only weigh the hero against a field when it also says what
the average stack is. Plenty of clients never report it.

Both are WRITTEN INTO THE SNAPSHOT in the host's own format (hand_body.write_stats
/ write_tournament) before it is parsed. That is the whole design: from there on
there is ONE body, so the felt, the exploit gate, the /move request and the
screen capture all read the same text and nothing downstream has to know a
number was typed rather than read.

Kept PER TABLE, never globally: an anonymising client reuses the same handful of
names at every table, and two tables are two tournaments at different stages.
The stats are never the hero's — the endpoint reads them to model the OPPONENTS.
"""
from __future__ import annotations

import json
from typing import Any

from .hand_body import (CORE_STATS, TOURNAMENT_KEYS, TOURNAMENT_WRITE_ORDER, stat_line,
                        tournament_phrase, write_stats, write_tournament)
from .jsnum import js_round, tidy, to_number

MANUAL_STAT_KEYS = CORE_STATS
"""What can be typed: the four the HUD leads with, in the pods' own order."""

# Names kept per table, oldest first out. A seat that is edited again is
# re-inserted at the end, so the cap only ever drops reads that have been sitting
# untouched for dozens of villains. A bound on a map nothing else prunes.
MAX_NAMES = 64

# What each header field accepts, and how it is rounded. `playersPaid` is NOT
# capped by `playersLeft` — in the money there are fewer players left than
# places paid, which is exactly the spot this exists for.
_TOURNAMENT_FIELDS = {
    "playersLeft": {"min": 1, "max": 1e7, "decimals": 0},
    "playersPaid": {"min": 1, "max": 1e7, "decimals": 0},
    "averageStack": {"min": 0.1, "max": 1e5, "decimals": 1},
}


def _blank(value: Any) -> bool:
    return value is None or value == ""


def _clean_stat(value: Any) -> float | None:
    """All four are percentages — the ratio stats (AF) are not among them."""
    n = to_number(value)
    if n is None or n in (float("inf"), float("-inf")):
        return None
    return tidy(min(100, max(0, js_round(n * 10) / 10)))


def _clean_tournament(key: str, value: Any) -> float | None:
    field = _TOURNAMENT_FIELDS.get(key)
    n = to_number(value)
    if not field or n is None or n in (float("inf"), float("-inf")):
        return None
    p = 10 ** field["decimals"]
    rounded = js_round(n * p) / p
    if rounded < field["min"] or rounded > field["max"]:
        return None
    return tidy(rounded)


class ManualStats:
    """`{'3': {'Big Stack Bob': {'VPIP': 38, 'PFR': 12}}}` — table index, name."""

    def __init__(self, raw: Any = None) -> None:
        self.tables: dict[str, dict[str, dict]] = {}
        if not isinstance(raw, dict):
            return
        for table, by_name in raw.items():
            if not isinstance(by_name, dict):
                continue
            seats = {}
            for name, stats in list(by_name.items())[-MAX_NAMES:]:
                entry = self._clean_entry(stats)
                if entry:
                    seats[str(name)] = entry
            if seats:
                self.tables[str(table)] = seats

    @staticmethod
    def _clean_entry(raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        out = {}
        for key in MANUAL_STAT_KEYS:
            v = _clean_stat(raw.get(key)) if not _blank(raw.get(key)) else None
            if v is not None:
                out[key] = v
        return out or None

    def table(self, index: int) -> dict[str, dict]:
        """Every typed stat at one table — the map write_stats takes."""
        return self.tables.get(str(index)) or {}

    def set(self, index: int, name: str, key: str, value: Any) -> bool:
        """Type one stat, or clear it with '' / None. True when it changed.

        A seat left with nothing typed is removed rather than kept as an empty
        object, so "has the operator said anything about this villain" stays a
        question about whether the key is there.
        """
        if key not in MANUAL_STAT_KEYS:
            return False
        table = str(index)
        seats = dict(self.tables.get(table) or {})
        before = seats.get(name)
        nxt = dict(before or {})
        v = None if _blank(value) else _clean_stat(value)
        if v is None:
            nxt.pop(key, None)
        else:
            nxt[key] = v

        # Re-inserted rather than updated in place: it is what keeps the cap
        # dropping the reads nobody has touched instead of the one being typed.
        seats.pop(name, None)
        if nxt:
            # In CORE_STATS order, so the body reads the way the HUD writes it.
            seats[name] = {k: nxt[k] for k in MANUAL_STAT_KEYS if k in nxt}
        for stale in list(seats)[: max(0, len(seats) - MAX_NAMES)]:
            del seats[stale]

        if seats:
            self.tables[table] = seats
        else:
            self.tables.pop(table, None)
        return seats.get(name) != before

    def clear(self, index: int, name: str) -> bool:
        """Forget everything typed for one seat — the villain who left, or a mistake."""
        seats = self.tables.get(str(index))
        if not seats or name not in seats:
            return False
        del seats[name]
        if not seats:
            del self.tables[str(index)]
        return True

    def apply(self, index: int, body: str) -> str:
        """The snapshot as it should go out: the host's text with the typed stats in it."""
        seats = self.table(index)
        return write_stats(body, seats) if seats else str(body)

    def to_wire(self, index: int) -> dict:
        """Per seat: the values, and the lines they add to every snapshot — the
        body's own syntax, verbatim, which is what the editor's footer shows."""
        return {
            name: {"values": dict(values),
                   "lines": [stat_line(k, values[k]) for k in MANUAL_STAT_KEYS if k in values]}
            for name, values in self.table(index).items()
        }

    def to_store(self) -> dict:
        return json.loads(json.dumps(self.tables))


class ManualTournament:
    """`{'3': {'playersLeft': 781, 'playersPaid': 92, 'averageStack': 78.9}}`."""

    def __init__(self, raw: Any = None) -> None:
        self.tables: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return
        for table, values in raw.items():
            entry = self._clean_entry(values)
            if entry:
                self.tables[str(table)] = entry

    @staticmethod
    def _clean_entry(raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        out = {}
        for key in TOURNAMENT_KEYS:
            v = None if _blank(raw.get(key)) else _clean_tournament(key, raw.get(key))
            if v is not None:
                out[key] = v
        return out or None

    def values(self, index: int) -> dict | None:
        return self.tables.get(str(index))

    def set(self, index: int, key: str, value: Any) -> bool:
        """Type one field, or clear it with '' / None. True when it changed.

        Cleared rather than zeroed: an empty field is "leave this one to the
        host", and the body is rewritten from the host's own text on every
        snapshot, so clearing genuinely hands the number back."""
        if key not in TOURNAMENT_KEYS:
            return False
        table = str(index)
        before = self.tables.get(table)
        nxt = dict(before or {})
        v = None if _blank(value) else _clean_tournament(key, value)
        if v is None:
            nxt.pop(key, None)
        else:
            nxt[key] = v
        ordered = {k: nxt[k] for k in TOURNAMENT_KEYS if k in nxt}
        if ordered:
            self.tables[table] = ordered
        else:
            self.tables.pop(table, None)
        return self.tables.get(table) != before

    def clear(self, index: int) -> bool:
        """Forget the whole header for one table — a new tournament at the same seat."""
        return self.tables.pop(str(index), None) is not None

    def apply(self, index: int, body: str) -> str:
        values = self.values(index)
        return write_tournament(body, values) if values else str(body)

    def to_wire(self, index: int) -> dict | None:
        values = self.values(index)
        if not values:
            return None
        return {"values": dict(values),
                "phrases": [tournament_phrase(k, values[k]) for k in TOURNAMENT_WRITE_ORDER
                            if k in values]}

    def to_store(self) -> dict:
        return json.loads(json.dumps(self.tables))
