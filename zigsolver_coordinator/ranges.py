"""The ranges a solve ran on, and the hand's history of them.

`meta.ranges` is one entry per seat of the game that was SOLVED, each with a
class map — `{"AA": 1, "AKs": 0.85, ...}` — that the API has already averaged
over the combos the board leaves live. It is absent wherever the answer was not
computed from enumerated ranges — both preflop engines and the exploit regime,
which never builds villain's range at all.

The STREETS are collected here rather than asked for: every street of a hand is
a separate /move call, and each one reports the ranges it was actually answered
on. Keeping them as they arrive is what makes "how did the turn card narrow
this" a thing you can look at, and it costs no extra solve — the alternative,
asking the endpoint for every street on every call, would price a narrowing
nobody has asked to see.

How a range is DRAWN (the 13x13 chart, live combos) is the front end's business
and stays there.
"""
from __future__ import annotations

import re
import time
from typing import Any

STREETS = ["preflop", "flop", "turn", "river"]


def board_cards(board: Any) -> list[str]:
    """'Ks 8d 3d 7c' -> ['Ks', '8d', '3d', '7c']. Tolerates commas and lists."""
    if isinstance(board, list):
        return [str(c) for c in board if c]
    return [t for t in re.split(r"[\s,]+", str(board or "")) if t]


def _number_or_zero(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return n if n == n else 0


def read_ranges(result: dict | None) -> dict | None:
    """`meta.ranges` off an answer, or None.

    Shape-checked rather than trusted: an older API answers without the block at
    all, and the front end's empty state is a sentence about the regime, not a
    crash.
    """
    meta = (result or {}).get("meta") or {}
    block = meta.get("ranges") if isinstance(meta, dict) else None
    if not isinstance(block, dict):
        return None
    players = block.get("players")
    if not isinstance(players, list) or not players:
        return None
    out_players = []
    for i, p in enumerate(players):
        p = p if isinstance(p, dict) else {}
        fallback = f"#{i}"
        weights = p.get("weights")
        out_players.append({
            "key": p.get("name") or p.get("label") or fallback,
            "name": p.get("name") or p.get("label") or fallback,
            "label": p.get("label") or p.get("name") or fallback,
            "position": p.get("position") or None,
            "seat": p.get("seat") or None,
            "hero": bool(p.get("hero")),
            "combos": _number_or_zero(p.get("combos")),
            "widthPct": _number_or_zero(p.get("widthPct")),
            "weights": weights if isinstance(weights, dict) else {},
        })
    board = block.get("board")
    return {
        "source": block.get("source") or None,
        "rootedAt": block.get("rootedAt") or None,
        "board": board_cards(board if board is not None else meta.get("board")),
        "players": out_players,
    }


def _street_index(street: Any) -> int:
    try:
        return STREETS.index(str(street))
    except ValueError:
        return len(STREETS)


def range_record(result: dict | None, at: int | None = None) -> dict | None:
    """One street's record off an answer, or None when it carries no ranges.

    Shared with the solve history, which rebuilds a past hand's streets from the
    answers it kept rather than from the live store below.
    """
    if not result or result.get("type") != "answer":
        return None
    ranges = read_ranges(result)
    if not ranges:
        return None
    return {
        "street": result.get("street") or ranges["rootedAt"] or "flop",
        "at": at if at is not None else int(time.time() * 1000),
        "solver": result.get("solver") or None,
        **ranges,
    }


def sort_streets(records: list[dict]) -> list[dict]:
    """Records in street order, earliest first."""
    return sorted(records, key=lambda r: _street_index(r["street"]))


class HandRanges:
    """One table's live hand, and one record per street of it that came back
    carrying ranges.

    Keyed by handId so a new hand replaces rather than accumulates — and a
    street re-solved (a re-read, a regime change, a stat typed) overwrites its
    own record, since the question was re-asked and the old ranges are no longer
    the ones behind the answer on screen.
    """

    def __init__(self) -> None:
        self.hand_id: str | None = None
        self._streets: dict[str, dict] = {}

    def record(self, hand_id: str | None, result: dict) -> bool:
        """File an answer's ranges under the hand it was asked about."""
        record = range_record(result)
        if not record:
            return False
        hand = hand_id or "unknown"
        if self.hand_id != hand:
            self.hand_id = hand
            self._streets = {}
        self._streets[record["street"]] = record
        return True

    def streets(self) -> list[dict]:
        return sort_streets(list(self._streets.values()))

    def clear(self) -> bool:
        had = bool(self._streets) or self.hand_id is not None
        self.hand_id = None
        self._streets = {}
        return had
