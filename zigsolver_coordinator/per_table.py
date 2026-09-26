"""The regime and the preflop engine, kept per table.

Both pickers sit on a table's bar and are about the hand on THAT felt: which
question it is asked, which engine plays its preflop. So each table index keeps
its own — Exploit picked on one table is not what another plays, and an
Advanced mix set on one does not weight the coin of another.

A table nobody has picked anything on starts from the TEMPLATE: the defaults,
or — on a data directory from before the pickers went per table — the one
choice every table shared then, so an upgrade changes nothing until a table is
changed. Nothing picks the template again; it is only where a table starts.

On disk it is the old single-choice format with the tables beside it, so an
older build still reads the template:

    {"selected": "gto", "exploitPct": 50, "requireStats": true,
     "tables": {"3": {"selected": "exploit", "exploitPct": 50, "requireStats": true}}}

A table whose state is the template is not stored: the absence of a key is the
absence of a choice.
"""
from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar

M = TypeVar("M")


class PerTable(Generic[M]):
    """One picker's state per table index. `make` is RegimeState or PreflopState:
    it builds a state from a stored value, sanitizing it, and `to_wire()` is what
    it is stored as."""

    def __init__(self, make: Callable[[Any], M], raw: Any = None) -> None:
        self._make = make
        self.template: dict = make(raw).to_wire()
        self._tables: dict[str, M] = {}
        saved = raw.get("tables") if isinstance(raw, dict) else None
        if isinstance(saved, dict):
            for key, value in saved.items():
                self._tables[str(key)] = make(value)

    def of(self, index: int) -> M:
        """This table's state. Made from the template on first use and the same
        object from then on, so a change made through it is kept."""
        key = str(index)
        state = self._tables.get(key)
        if state is None:
            state = self._tables[key] = self._make(dict(self.template))
        return state

    def to_store(self) -> dict:
        tables = {key: state.to_wire() for key, state in self._tables.items()}
        return {**self.template,
                "tables": {key: wire for key, wire in tables.items() if wire != self.template}}
