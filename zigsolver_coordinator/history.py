"""Every answer a table has been given, kept so a hand can be read again after
the table has moved past it.

The front end's panel only ever holds the newest answer: the turn replaces the
flop, the next hand replaces the river, and a spot worth a second look is gone
by the time anyone thinks to look. So each answer that lands — an error
included, since a refused body is usually a misread and worth seeing again — is
filed here with the snapshot it was asked on.

What is stored is the answer object itself (answers.build_answer). It already
carries the request, and so the exact body that went out, plus the raw payload
with `meta.ranges` — which is how a past hand's range streets are rebuilt
without keeping a second copy of them.

The hand's LAST snapshot is kept too, when the host sends one: the body that ends
on "Hand finished". The hero's decisions stop at the hero's last action, and
without it the history would never show what the villains did next, the river
that came, or the cards shown down.

Kept PER TABLE, one file each in the data directory, newest first and capped.
"""
from __future__ import annotations

import secrets
import time
from typing import Any

from . import wire
from .hand_body import parse_hand_body
from .ranges import range_record, sort_streets
from .store import Store

PER_TABLE = 150
"""Per table. A session of a few hours is well inside it."""


def _valid(e: Any) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not isinstance(e.get("at"), (int, float)):
        return False
    if e.get("kind") == "final":
        return isinstance(e.get("body"), str)
    result = e.get("result")
    return (isinstance(result, dict) and result.get("type") in ("answer", "error")
            and isinstance((result.get("request") or {}).get("body"), str))


def body_of(e: dict) -> str:
    """The snapshot an entry is about — what was asked, or how the hand ended."""
    return e["body"] if e.get("kind") == "final" else e["result"]["request"]["body"]


def _is_solve(e: dict) -> bool:
    return e.get("kind") != "final"


def _summarize(body: str) -> dict:
    """The few things the list shows, read once when the answer is filed rather
    than by parsing every body each time the sheet opens."""
    parsed = parse_hand_body(body)
    if not parsed:
        return {"street": None, "heroHand": None, "board": [], "pot": None}
    return {
        "street": parsed["streetName"] or None,
        "heroHand": list(parsed["heroHand"]) if parsed["heroHand"] else None,
        "board": list(parsed["board"] or []),
        "pot": parsed["pot"],
    }


def _compact(entry: dict) -> dict:
    """What goes to disk. An answer's `meta` and `warnings` are `payload.meta`
    again, and the ranges in it are the bulk of an entry — so they are written
    once and rebuilt on load."""
    result = entry.get("result")
    if not isinstance(result, dict) or result.get("type") != "answer":
        return entry
    return {**entry, "result": {k: v for k, v in result.items() if k not in ("meta", "warnings")}}


def _expand(entry: dict) -> dict:
    result = entry.get("result")
    if isinstance(result, dict) and result.get("type") == "answer" and "meta" not in result:
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        result["meta"] = meta
        result["warnings"] = meta.get("warnings") or []
    return entry


def _new_id() -> str:
    return f"{int(time.time() * 1000):x}-{secrets.token_hex(3)}"


def _regime_of(result: dict) -> str | None:
    return result.get("regime") if result.get("type") == "answer" else (result.get("request") or {}).get("regime")


class History:
    def __init__(self, store: Store) -> None:
        self.store = store
        # Minted once per process and put in front of every handId. The handId
        # count restarts with the process (`t3#1-AhKd`), so on its own it would
        # file tonight's first hand under last night's whenever the hero
        # happened to hold the same two cards.
        self.session = secrets.token_hex(3)
        self.tables: dict[str, list[dict]] = {}
        self._revs: dict[str, int] = {}
        for table, entries in store.history_files().items():
            if isinstance(entries, list):
                kept = [_expand(e) for e in entries if _valid(e)][:PER_TABLE]
                if kept:
                    self.tables[str(table)] = kept

    # --- bookkeeping ------------------------------------------------------------

    def _file(self, table: str) -> str:
        return f"history/{table}.json"

    def _save(self, table: str) -> None:
        self._revs[table] = self._revs.get(table, 0) + 1
        if self.tables.get(table):
            self.store.write_soon(self._file(table), lambda t=table: [_compact(e) for e in self.tables.get(t, [])])
        else:
            self.store.remove(self._file(table))

    def rev(self, index: int) -> int:
        """Bumped on every change to one table's history — the front end's cue to
        re-read whatever it is showing of it."""
        return self._revs.get(str(index), 0)

    def _hand_key(self, hand_id: str | None) -> str:
        return f"{self.session}:{hand_id or '?'}"

    # --- filing -----------------------------------------------------------------

    def record_solve(self, index: int, hand_id: str | None, result: dict) -> bool:
        """File the answer that just landed, under the hand it was asked for.

        The same body answered in the same regime again — Re-solve, or a settings
        change — REPLACES the entry it repeats: it is the same decision, and a
        list of five identical flops is harder to read than one. A different
        regime on the same body is a different question and gets its own row.
        """
        body = ((result or {}).get("request") or {}).get("body")
        if not body or result.get("type") not in ("answer", "error"):
            return False
        table = str(index)
        entries = self.tables.get(table, [])
        entry = {
            "id": _new_id(),
            "at": int(time.time() * 1000),
            "hand": self._hand_key(hand_id),
            **_summarize(body),
            "result": wire.copy(result),
        }
        top = next((e for e in entries if _is_solve(e)), None)
        # Only the newest row can be repeated: once the hand has ended, a re-solve
        # of its last decision is a new row after the ending rather than a rewrite.
        repeats = (top is not None and entries and top is entries[0]
                   and top["hand"] == entry["hand"]
                   and top["result"]["request"]["body"] == body
                   and _regime_of(top["result"]) == _regime_of(entry["result"]))
        self.tables[table] = [entry, *(entries[1:] if repeats else entries)][:PER_TABLE]
        self._save(table)
        return True

    def record_hand_end(self, index: int, hand_id: str | None, body: str | None) -> bool:
        """File the snapshot a hand ended on, against the hand it ended.

        Only for a hand this table has answered something in — a result with no
        decision behind it is not a hand the history has a place for. A hand
        ends once, so a second finish for it replaces the first.
        """
        if not hand_id or not body:
            return False
        table = str(index)
        entries = self.tables.get(table, [])
        hand = self._hand_key(hand_id)
        if not any(_is_solve(e) and e["hand"] == hand for e in entries):
            return False
        entry = {"id": _new_id(), "at": int(time.time() * 1000), "hand": hand, "kind": "final",
                 **_summarize(body), "body": body}
        rest = [e for e in entries if not (e.get("kind") == "final" and e["hand"] == hand)]
        self.tables[table] = [entry, *rest][:PER_TABLE]
        self._save(table)
        return True

    def clear(self, index: int) -> bool:
        """Forget one table's history."""
        table = str(index)
        had = bool(self.tables.pop(table, None))
        self._save(table)
        return had

    # --- reading ----------------------------------------------------------------

    def entries_for(self, index: int) -> list[dict]:
        """This table's decisions, newest first — the answers, not how hands ended."""
        return [e for e in self.tables.get(str(index), []) if _is_solve(e)]

    def count(self, index: int) -> int:
        return len(self.entries_for(index))

    def find_entry(self, index: int, entry_id: Any) -> dict | None:
        if not entry_id:
            return None
        return next((e for e in self.entries_for(index) if e["id"] == entry_id), None)

    def hands_for(self, index: int) -> list[dict]:
        """This table's hands, newest first, each with its decisions in the order
        they were answered and the snapshot it ended on, if one came.

        `last` is the fullest account of the hand there is: how it ended when
        that is known, and otherwise the last decision asked.
        """
        hands: list[dict] = []
        by_key: dict[str, dict] = {}
        for e in self.tables.get(str(index), []):
            h = by_key.get(e["hand"])
            if h is None:
                h = {"key": e["hand"], "at": e["at"], "heroHand": None, "entries": [], "final": None}
                by_key[e["hand"]] = h
                hands.append(h)
            if e.get("kind") == "final":
                h["final"] = e
            else:
                h["entries"].insert(0, e)
            if not h["heroHand"] and e.get("heroHand"):
                h["heroHand"] = e["heroHand"]
        out = [h for h in hands if h["entries"]]
        for h in out:
            h["last"] = h["final"] or h["entries"][-1]
        return out

    def find_hand(self, index: int, key: Any) -> dict | None:
        return next((h for h in self.hands_for(index) if h["key"] == key), None)

    def ranges_at(self, index: int, entry: dict | None) -> list[dict]:
        """The ranges a past decision could have been read against: its hand's
        streets as they stood when it was answered, latest record per street.

        Nothing answered AFTER it — reviewing the flop should not show a turn
        range the flop could not have known about.
        """
        if not entry:
            return []
        streets: dict[str, dict] = {}
        same = [e for e in self.entries_for(index) if e["hand"] == entry["hand"] and e["at"] <= entry["at"]]
        for e in reversed(same):
            record = range_record(e["result"], e["at"])
            if record:
                streets[record["street"]] = record
        return sort_streets(list(streets.values()))

    # --- what the front end is sent ---------------------------------------------------

    @staticmethod
    def _row(e: dict) -> dict:
        """One decision as the list draws it: what was played, in which regime."""
        result = e["result"]
        sampled = result.get("sampled") if result.get("type") == "answer" else None
        return {
            "id": e["id"], "at": e["at"], "street": e.get("street"), "board": e.get("board") or [],
            "heroHand": e.get("heroHand"), "pot": e.get("pot"),
            "result": {
                "type": result.get("type"),
                "regime": result.get("regime"),
                "sampled": {"action": sampled.get("action")} if isinstance(sampled, dict) else None,
                "request": {"regime": (result.get("request") or {}).get("regime")},
            },
        }

    @staticmethod
    def _last(h: dict) -> dict:
        last = h["last"]
        return {"street": last.get("street"), "pot": last.get("pot"), "board": last.get("board") or []}

    def list_wire(self, index: int) -> dict:
        """Every hand, light: enough for the list, nothing it does not draw."""
        hands = self.hands_for(index)
        return {
            "hands": [{"key": h["key"], "at": h["at"], "heroHand": h["heroHand"],
                       "final": h["final"] is not None, "last": self._last(h),
                       "entries": [self._row(e) for e in h["entries"]]} for h in hands],
            "decisions": sum(len(h["entries"]) for h in hands),
        }

    def hand_wire(self, index: int, key: Any) -> dict | None:
        """One hand in full: its record parsed, every answer, and the ranges each
        decision could have been read against."""
        h = self.find_hand(index, key)
        if not h:
            return None
        return {
            "key": h["key"], "at": h["at"], "heroHand": h["heroHand"],
            "final": h["final"] is not None, "last": self._last(h),
            "record": parse_hand_body(body_of(h["last"])),
            "ranges": self.ranges_at(index, h["entries"][-1]),
            "entries": [{**self._row(e), "result": e["result"], "ranges": self.ranges_at(index, e)}
                        for e in h["entries"]],
        }

    def entry_wire(self, index: int, entry_id: Any) -> dict | None:
        """One decision, for putting back on the felt — with its neighbours, so
        the front end can step older and newer without asking for the list."""
        entries = self.entries_for(index)
        at = next((i for i, e in enumerate(entries) if e["id"] == entry_id), -1)
        if at < 0:
            return None
        e = entries[at]
        return {
            "id": e["id"], "at": e["at"], "street": e.get("street"),
            "result": e["result"],
            "hand": parse_hand_body(body_of(e)),
            "ranges": self.ranges_at(index, e),
            "older": entries[at + 1]["id"] if at + 1 < len(entries) else None,
            "newer": entries[at - 1]["id"] if at > 0 else None,
        }
