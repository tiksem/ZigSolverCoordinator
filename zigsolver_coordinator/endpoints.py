"""The two hosts the coordinator talks to, and how to address them.

  bot host   the Kotlin runner. One socket per table, mode=0: it pushes the
             table snapshots and takes the commands. Also serves the table's
             screenshot (`/image/N`) and the screenshot check upload.
  ZigSolver  the solver API. Every snapshot is POSTed to its /move.

Both are typed once on the front end's root view — `localhost:8080`,
`http://10.0.0.4:8000`, `ws://box:8080/app`, all fine — and kept here.

The API may want a bearer token (`api/server.py --auth-token`). It is attached
in exactly ONE place, `api_headers`, and only to calls at the API: the bot host
is a host the operator typed, and sending the solver's credential there would be
handing it to a third party.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

INDEXES_TABLE_INDEX = 96782
"""The table index the Kotlin side broadcasts the running-table list on."""

MODE_HAND = 0
"""ConnectionMode.entries — 0 is HAND: snapshots and commands."""

MAX_INPUT = 200


@dataclass(frozen=True)
class Target:
    secure: bool
    host: str
    prefix: str


def parse_server(text: Any) -> Target | None:
    """Free-form input -> Target, or None."""
    text = str(text or "").strip()
    if not text:
        return None
    with_scheme = text if re.match(r"^[a-z]+://", text, re.I) else f"http://{text}"
    try:
        url = urllib.parse.urlsplit(with_scheme)
        url.port  # raises on a port that is not one
    except ValueError:
        return None
    if not url.hostname or re.search(r"\s", url.netloc):
        return None
    host = url.netloc.rsplit("@", 1)[-1]
    prefix = url.path.rstrip("/")
    return Target(secure=url.scheme.lower() in ("https", "wss"), host=host, prefix=prefix)


def _http(target: Target | None, path: str) -> str | None:
    if not target:
        return None
    scheme = "https" if target.secure else "http"
    return f"{scheme}://{target.host}{target.prefix}{path}"


class Endpoints:
    def __init__(self, raw: Any = None) -> None:
        raw = raw if isinstance(raw, dict) else {}
        self.host_input = self._text(raw.get("host"))
        self.api_input = self._text(raw.get("api"))
        # OPTIONAL: empty means the API wants none, which is the usual case on a
        # LAN — and an empty value sends no header rather than an empty one.
        self.api_token = self._text(raw.get("apiToken"), 512)
        # Whether the operator asked to be connected: the lobby socket follows it.
        self.connected = bool(raw.get("connected")) and self.host is not None

    @staticmethod
    def _text(value: Any, cap: int = MAX_INPUT) -> str:
        return str(value).strip()[:cap] if isinstance(value, str) else ""

    @property
    def host(self) -> Target | None:
        return parse_server(self.host_input)

    @property
    def api(self) -> Target | None:
        return parse_server(self.api_input)

    def update(self, *, host: Any = None, api: Any = None, api_token: Any = None) -> set[str]:
        """Change what was given; the names of what actually changed."""
        changed = set()
        if host is not None and self._text(host) != self.host_input:
            self.host_input = self._text(host)
            changed.add("host")
        if api is not None and self._text(api) != self.api_input:
            self.api_input = self._text(api)
            changed.add("api")
        if api_token is not None and self._text(api_token, 512) != self.api_token:
            self.api_token = self._text(api_token, 512)
            changed.add("apiToken")
        return changed

    def socket_url(self, mode: int, table_index: int) -> str | None:
        target = self.host
        if not target:
            return None
        scheme = "wss" if target.secure else "ws"
        return f"{scheme}://{target.host}{target.prefix}/commands?mode={mode}&tableIndex={table_index}"

    def http_url(self, path: str) -> str | None:
        """A URL on the bot host (/image/N, /checkScreenshot)."""
        return _http(self.host, path)

    def api_url(self, path: str) -> str | None:
        """A URL on the ZigSolver API (/move, /cancel, /health, /screenError)."""
        return _http(self.api, path)

    def api_headers(self, headers: dict | None = None) -> dict:
        """`headers` plus the API's Authorization, when there is a token. Every
        call at the API goes through this, and nothing else does."""
        out = dict(headers or {})
        if self.api_token:
            out["Authorization"] = f"Bearer {self.api_token}"
        return out

    @staticmethod
    def display(target: Target | None) -> str:
        return f"{target.host}{target.prefix}" if target else ""

    def to_wire(self) -> dict:
        """What the front end is told. Never the token itself — only whether one
        is set: any page that can reach this coordinator would otherwise be able
        to read the solver's credential off the socket."""
        return {
            "host": self.host_input,
            "api": self.api_input,
            "apiTokenSet": bool(self.api_token),
            "hostValid": self.host is not None,
            "apiValid": self.api is not None,
            "hostDisplay": self.display(self.host),
            "apiDisplay": self.display(self.api),
            "connected": self.connected,
        }

    def to_store(self) -> dict:
        return {"host": self.host_input, "api": self.api_input,
                "apiToken": self.api_token, "connected": self.connected}
