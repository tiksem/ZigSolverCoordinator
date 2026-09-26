"""The screen behind a call that failed.

When /move comes back an error the snapshot is usually not wrong about the SOLVE
— it is wrong about the TABLE. The host's extractor read a stack that isn't
there, dealt a card twice, or lost the block that says whose turn it is, and the
endpoint refused a body describing a table that cannot exist. The snapshot text
is the evidence of what was read; only the pixels show what there was to read.

So a failed call pulls the table's screenshot off the bot host —
`GET /image/{tableIndex}`, a PNG, optionally encrypted exactly like the socket
frames — and forwards it to the API's `POST /screenError`, which files it under
`screenerrors/` named for the hand and the moment (api/screenerror.py). The
failing body and the endpoint's own words go with it.

The image is NEVER touched: no decode, no re-encode, no resize. Decryption is
the only transform, and it is the one that yields the original bytes. Base64 is
transport — the API decodes it back to the same bytes and writes those. What a
capture is read for is a rank the extractor got wrong or a stack digit it
misread, and those are a few pixels tall.

Three rules, all of them about not making a bad moment worse:

  * every failure here is swallowed. The operator has already been shown the
    error that matters; a capture that could not be taken is not a second one.
  * one capture at a time. A host mid-breakage fails every snapshot, and a
    screenshot is megabytes — the captures would outweigh the solves.
  * the same hand failing the same way twice is captured once. Auto-solve
    re-sends on every re-read, and those are all pictures of one bug.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .bot_host import fetch_image
from .crypto import looks_like_png
from .endpoints import Endpoints
from .solver_api import SolverApi

log = logging.getLogger("zsc.screen")

MAX_KEYS = 400
"""Forget the oldest keys past this, so a long session cannot grow forever."""


def error_text(value: Any) -> str | None:
    """An error's `message`/`hint` -> one line of text.

    Either the endpoint's own words (a string) or one of ours as a `{key,
    params}` pair, deliberately untranslated. The record must not be in whatever
    language a front end happened to be in, so the pair is written out as the
    key it is rather than resolved.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("key"):
        params = value.get("params")
        return f"{value['key']} {json.dumps(params)}" if params else value["key"]
    return str(value)


class ScreenErrors:
    def __init__(self, http: httpx.AsyncClient, endpoints: Endpoints, api: SolverApi) -> None:
        self.http = http
        self.endpoints = endpoints
        self.api = api
        self._captured: dict[str, None] = {}
        self._busy = False

    async def capture(self, failure: dict) -> dict:
        """Capture the table behind one failed call. Never raises; returns

            {'ok': True, 'bytes': n, 'png': bool, 'file': ...}   stored
            {'ok': False, 'reason': '...'}                       not, and why
        """
        table_index = failure.get("tableIndex")
        if table_index is None:
            return {"ok": False, "reason": "no table"}
        error = error_text(failure.get("error"))

        key = f"{failure.get('handId') or ''} {error or ''}"
        if key in self._captured:
            return {"ok": False, "reason": "already captured"}
        if self._busy:
            return {"ok": False, "reason": "a capture is already running"}
        if not self.endpoints.api_url("/screenError"):
            return {"ok": False, "reason": "no API"}

        # Claimed before the first await: two failures landing together must not
        # both decide they are the one to run.
        self._busy = True
        self._captured[key] = None
        if len(self._captured) > MAX_KEYS:
            self._captured.pop(next(iter(self._captured)))

        try:
            image = await fetch_image(self.http, self.endpoints, table_index)
            if not image:
                return {"ok": False, "reason": "the host sent no image"}
            res = await self.api.screen_error({
                "image": base64.b64encode(image).decode("ascii"),
                "handId": failure.get("handId"),
                "tableIndex": table_index,
                "requestId": failure.get("requestId"),
                "regime": failure.get("regime"),
                "street": failure.get("street"),
                "error": error,
                "hint": error_text(failure.get("hint")),
                "status": failure.get("status"),
                "body": failure.get("body"),
                "clientAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            })
            if not res.is_success:
                # A capture the API refused is one we can take again once whatever
                # it objected to changes — a full disk, a body over its ceiling.
                self._captured.pop(key, None)
                said = res.text[:300]
                return {"ok": False,
                        "reason": f"{res.status_code} {res.reason_phrase}{f' — {said}' if said else ''}"}
            try:
                out = res.json()
            except ValueError:
                out = {}
            return {"ok": True, "bytes": len(image), "png": looks_like_png(image),
                    "file": out.get("file") if isinstance(out, dict) else None}
        except Exception as exc:  # noqa: BLE001 — every failure here is swallowed
            self._captured.pop(key, None)
            return {"ok": False, "reason": str(exc) or type(exc).__name__}
        finally:
            self._busy = False
