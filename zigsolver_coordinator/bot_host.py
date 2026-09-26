"""Everything that talks to the bot host.

  HostSocket        one reconnecting WebSocket — the lobby's, or one table's
  fetch_image       `GET /image/{tableIndex}`, for the screen behind a failure
  check_screenshot  `POST /checkScreenshot`, relayed for the front end

The socket behaves the way the browser's did, on purpose, because the hosts on
the other end were written against browsers: it sends no keepalive pings (the
mock and the fakebot never answer one, and a Python client that expects a pong
would drop a perfectly healthy socket after forty seconds), it offers no origin,
and it reconnects with a backoff that grows instead of hammering a dead host
every second forever.

Frames go through crypto.decrypt_message on the way in, so a host that encrypts
and one that doesn't both hand the same plain text to `on_message` — in arrival
order, one at a time, which the table relies on: it compares each snapshot
against the one before it to spot a partial re-read.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

import httpx
from websockets.asyncio.client import connect
from websockets.protocol import State

from . import __version__
from .crypto import decrypt_image, decrypt_message
from .endpoints import Endpoints

log = logging.getLogger("zsc.host")

BASE_DELAY = 1.0
MAX_DELAY = 15.0

STATUSES = ["idle", "connecting", "open", "retrying", "closed"]

USER_AGENT = f"zigsolver-coordinator/{__version__}"


class HostSocket:
    def __init__(self, url: Callable[[], str | None], on_message: Callable[[str], None],
                 on_status: Callable[[str], None] | None = None, *, name: str = "") -> None:
        self._url = url
        self._on_message = on_message
        self._on_status = on_status
        self.name = name
        self.status = "idle"
        self.attempts = 0
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._outbox: asyncio.Queue[str] | None = None
        self._ws = None

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            if self._on_status:
                self._on_status(status)

    def open(self) -> None:
        """Start connecting — or restart, if already running."""
        self._stop_task()
        if not self._url():
            self._set_status("idle")
            return
        self._task = asyncio.create_task(self._run(), name=f"host-socket {self.name}")

    def close(self) -> None:
        self._stop_task()
        self.attempts = 0
        self._set_status("closed")

    def reconnect(self) -> None:
        self.attempts = 0
        self.open()

    def _stop_task(self) -> None:
        task, self._task = self._task, None
        self._ws = None
        self._outbox = None
        if task is not None and not task.done():
            task.cancel()

    def send(self, text: str) -> bool:
        """Queue one frame; False when the socket is not open (the command is
        dropped, and the caller says so)."""
        ws, outbox = self._ws, self._outbox
        if ws is None or outbox is None or ws.state is not State.OPEN:
            return False
        outbox.put_nowait(str(text))
        return True

    async def _run(self) -> None:
        while True:
            url = self._url()
            if not url:
                self._set_status("idle")
                return
            self._set_status("retrying" if self.attempts else "connecting")
            try:
                async with connect(url, ping_interval=None, close_timeout=2, open_timeout=10,
                                   max_size=32 * 2**20, proxy=None,
                                   user_agent_header=USER_AGENT) as ws:
                    outbox: asyncio.Queue[str] = asyncio.Queue()
                    self._ws, self._outbox = ws, outbox
                    self.attempts = 0
                    self.last_error = None
                    self._set_status("open")
                    log.info("%s: connected to %s", self.name, url)
                    writer = asyncio.create_task(self._write(ws, outbox))
                    try:
                        async for message in ws:
                            self._deliver(message)
                    finally:
                        writer.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure is a retry
                self.last_error = str(exc) or type(exc).__name__
                log.info("%s: %s", self.name, self.last_error)
            finally:
                self._ws = None
                self._outbox = None
            self.attempts += 1
            self._set_status("retrying")
            await asyncio.sleep(min(MAX_DELAY, BASE_DELAY * 2 ** min(self.attempts - 1, 4)))

    def _deliver(self, message: str | bytes) -> None:
        # One snapshot that trips a bug must not cost the socket: log it and go on.
        try:
            self._on_message(decrypt_message(message))
        except Exception:  # noqa: BLE001
            log.exception("%s: failed to handle a frame", self.name)

    @staticmethod
    async def _write(ws, outbox: asyncio.Queue[str]) -> None:
        while True:
            await ws.send(await outbox.get())


async def fetch_image(http: httpx.AsyncClient, endpoints: Endpoints, table_index: int,
                      timeout: float = 8.0) -> bytes | None:
    """The table's screenshot as the PNG bytes, or None when there is no host.

    Longer than a normal request because the host takes the screenshot on
    demand, and shorter than forever because this is a diagnostic, not the thing
    the operator is waiting for. Raises on a host that will not serve it.
    """
    url = endpoints.http_url(f"/image/{table_index}")
    if not url:
        return None
    res = await http.get(url, timeout=timeout)
    if not res.is_success:
        raise RuntimeError(f"GET /image/{table_index}: {res.status_code} {res.reason_phrase}")
    return decrypt_image(res.content)


async def check_screenshot(http: httpx.AsyncClient, endpoints: Endpoints, *, image: bytes,
                           filename: str, mime: str, check2: bool, crop: str,
                           table_index: str) -> dict:
    """`POST /checkScreenshot` — the same multipart the old check.html sent
    (image, check2, crop, tableIndex), in that order: the image part first, as a
    browser's FormData puts it."""
    url = endpoints.http_url("/checkScreenshot")
    if not url:
        raise ValueError("no bot host is configured")
    parts: list = [("image", (filename or "screenshot.png", image, mime or "application/octet-stream"))]
    if check2:
        parts.append(("check2", (None, "true")))
    if crop:
        parts.append(("crop", (None, crop)))
    if table_index != "":
        parts.append(("tableIndex", (None, table_index)))
    res = await http.post(url, files=parts, timeout=120)
    content_type = res.headers.get("content-type", "")
    return {"ok": res.is_success, "status": res.status_code, "statusText": res.reason_phrase,
            "contentType": content_type, "body": res.content}
