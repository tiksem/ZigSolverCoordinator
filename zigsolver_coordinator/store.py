"""Files in the data directory, in one place.

Everything the operator sets by hand survives a restart — the endpoints, the
solve settings, the regime, the typed stats and headers — and so does the solve
history. The front end used to keep all of it in localStorage, one browser at a
time; here there is one copy, whichever page asked for the change.

Two rules the callers rely on:

  * a write that fails is not an error. A full disk or a read-only directory
    costs persistence, not the session: the coordinator keeps working and just
    forgets on restart. Nothing here ever propagates a storage failure.
  * a stored value is UNTRUSTED input. It was written by an older build, or
    hand-edited, so every owner sanitizes what it reads and falls back to its
    default rather than restoring nonsense.

Writes are debounced (a burst of keystrokes in a stats field is one write) and
atomic (a crash mid-write leaves the previous file, never half of one).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from .wire import dumps

log = logging.getLogger("zsc.store")

WRITE_DELAY = 0.3


class Store:
    def __init__(self, data_dir: Path | str) -> None:
        self.dir = Path(data_dir).expanduser()
        self._pending: dict[Path, tuple[Callable[[], Any], bool]] = {}
        self._handle: asyncio.TimerHandle | None = None
        try:
            (self.dir / "history").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create %s (%s) — nothing will persist", self.dir, exc)

    def path(self, name: str) -> Path:
        return self.dir / name

    def read(self, name: str) -> Any:
        """The stored JSON under `name`, or None when absent or unreadable."""
        try:
            return json.loads(self.path(name).read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self.path(name), exc)
            return None

    def history_files(self) -> dict[str, Any]:
        """`{'3': [...]}` for every table with a history file."""
        out = {}
        try:
            files = sorted((self.dir / "history").glob("*.json"))
        except OSError:
            return out
        for f in files:
            data = self.read(f"history/{f.name}")
            if data is not None:
                out[f.stem] = data
        return out

    def write_soon(self, name: str, producer: Callable[[], Any], *, private: bool = False) -> None:
        """Write `producer()` to `name` shortly; later calls for the same name win.

        `private` files are created 0600 — state.json holds the API token.
        Outside an event loop (a test, a shutdown) the write happens now.
        """
        self._pending[self.path(name)] = (producer, private)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        if self._handle is None:
            self._handle = loop.call_later(WRITE_DELAY, self.flush)

    def remove(self, name: str) -> None:
        path = self.path(name)
        self._pending.pop(path, None)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)

    def flush(self) -> None:
        """Write everything pending, now."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        pending, self._pending = self._pending, {}
        for path, (producer, private) in pending.items():
            try:
                self._write(path, dumps(producer()), private)
            except Exception as exc:  # noqa: BLE001 — never propagate a storage failure
                log.warning("could not write %s: %s", path, exc)

    @staticmethod
    def _write(path: Path, text: str, private: bool) -> None:
        tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
