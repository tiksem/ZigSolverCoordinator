"""Which engine answers a PREFLOP decision, and who decides.

A sibling of regime.py, deliberately separate rather than more entries in that
picker. The two answer different streets and neither constrains the other:
`regime` is the question asked about the FLOP onwards, and this is which of two
engines plays the hand before there is a board.

Three choices, two of which are the endpoint's `preflop` verbatim:

  alg       the rangegen chart: a fast heuristic bent by the opponents' stats
  gto       the presolved blueprint: an exact solve of the actual game
  advanced  neither — draw one per hand at a mix the operator set

The draw happens ONCE PER HAND, on the hand's FIRST PREFLOP DECISION, and is kept
for the rest of its preflop: opening under one engine only to face the 3-bet
under the other produces a line neither would have played.

`gto` NEEDS THE BLUEPRINT SERVICE. When the API reports it is not configured
(`/health.preflopEngines` without "gto"), this falls back to `alg` — and says so
rather than offering a choice that would quietly come back as the chart anyway.
"""
from __future__ import annotations

import random
from typing import Any

from .jsnum import is_finite_number, js_round, to_number

ENGINES = ["alg", "gto", "advanced"]


def _clamp_pct(value: Any) -> int:
    return min(100, max(0, js_round(to_number(value))))


class PreflopState:
    def __init__(self, raw: Any = None) -> None:
        self.selected = "alg"
        # Advanced: the weight on GTO, in percent. 0 = always the chart, 100 =
        # always the blueprint — both ends are legal, and are how you park it.
        self.gto_pct = 50
        if raw is None:
            return
        saved = raw if isinstance(raw, dict) else {"selected": raw}
        if saved.get("selected") in ENGINES:
            self.selected = saved["selected"]
        if saved.get("gtoPct") is not None and is_finite_number(saved["gtoPct"]):
            self.gto_pct = _clamp_pct(saved["gtoPct"])

    def select(self, value: Any) -> bool:
        if value not in ENGINES:
            return False
        changed = value != self.selected
        self.selected = value
        return changed

    def set_gto_pct(self, value: Any) -> bool:
        if not is_finite_number(value) or value is None or value == "":
            return False
        before = self.gto_pct
        self.gto_pct = _clamp_pct(value)
        return before != self.gto_pct

    def flip_coin(self) -> str:
        """The advanced mode's coin. Drawn ONCE PER HAND — the caller keeps it,
        and makes sure it is drawn on the hand's first preflop decision."""
        return "gto" if random.random() * 100 < self.gto_pct else "alg"

    def resolve(self, coin: str | None, available: bool) -> dict:
        """This hand's engine, resolved against what the server can serve.

        `forced` is set only where the fallback actually cost something — the
        coin came up `gto`, or `gto` was selected outright, and the service is
        not there. Choosing `alg` and getting `alg` is not a fallback."""
        want = coin if self.selected == "advanced" else self.selected
        if want == "gto" and not available:
            return {"coin": coin, "engine": "alg", "forced": {"key": "reason.noPreflopService"}}
        return {"coin": coin, "engine": want, "forced": None}

    def to_wire(self) -> dict:
        return {"selected": self.selected, "gtoPct": self.gto_pct}


def gto_available(health_info: Any) -> bool:
    """Whether the server can actually answer with the blueprint.

    An OLDER server does not send `/health.preflopEngines` at all; that is read
    as "chart only" rather than as "yes", because guessing yes would offer a
    choice every hand would silently ignore."""
    engines = health_info.get("preflopEngines") if isinstance(health_info, dict) else None
    return isinstance(engines, list) and "gto" in engines
