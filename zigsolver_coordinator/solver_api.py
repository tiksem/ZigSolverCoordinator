"""The ZigSolver API client.

Every snapshot is POSTed as the /move `body`, under one `regime`: `gto` (the
equilibrium strategy at the node) or `exploit` (the maximum-EV action against
the villain's measured behaviour). Never both — the endpoint answers one
question per call.

The one field worth knowing: `handId`. It keys the per-hand tree cache, so a
later street reuses the tree the earlier one built. It must be stable WITHIN a
hand and distinct ACROSS hands — a handId whose tree was solved for a different
hero hand keeps that tree. It does nothing for an exploit answer: that regime
walks the hand rather than a subgame and is recomputed every call, by design.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import httpx

from .answers import build_answer, build_error
from .endpoints import Endpoints

log = logging.getLogger("zsc.api")

# A solve takes as long as its budget, and the budget goes to ten minutes: the
# read timeout is off, and it is our own cancel that ends a solve nobody wants.
# The connect is generous too: a remote box now and then takes over 10s to
# answer the handshake, and a connect given up on is a decision lost.
MOVE_TIMEOUT = httpx.Timeout(connect=120.0, read=None, write=30.0, pool=None)
CANCEL_TIMEOUT = 5.0
HEALTH_TIMEOUT = 8.0
SCREEN_ERROR_TIMEOUT = 60.0

_TUNING = ["gateExploitability", "targetExploitability", "minSolveTime"]


def detail_of(payload: Any, fallback: str) -> Any:
    """Reads the endpoint's own explanation out of a non-200 body."""
    if not payload:
        return fallback
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return fallback
    d = next((payload[k] for k in ("detail", "error", "message") if payload.get(k) is not None), None)
    if isinstance(d, str):
        return d
    # FastAPI validation errors: [{loc, msg, type}, ...]
    if isinstance(d, list):
        parts = [f"{'.'.join(str(x) for x in (e.get('loc') or []))}: {e.get('msg')}"
                 for e in d if isinstance(e, dict)]
        return "; ".join(parts) or fallback
    return json.dumps(d) if d else fallback


def move_payload(req: dict) -> dict:
    """The request as the endpoint takes it — and only the fields that are set.

    `preflop` is omitted rather than defaulted so an older server, one that does
    not know the field, keeps its own default instead of being sent a name it
    would reject. The flop tuning is sent on a null check rather than a truthy
    one: gateExploitability 0 is a real setting (never solve exactly).
    """
    payload = {"body": req["body"], "regime": req.get("regime") or "gto"}
    for key in ("preflop", "handId", "requestId", "maxSolveTime", "statHands"):
        if req.get(key):
            payload[key] = req[key]
    for key in _TUNING:
        if req.get(key) not in (None, ""):
            payload[key] = req[key]
    return payload


class SolverApi:
    def __init__(self, http: httpx.AsyncClient, endpoints: Endpoints,
                 spawn: Callable[[Coroutine], Any]) -> None:
        self.http = http
        self.endpoints = endpoints
        self._spawn = spawn

    async def move(self, req: dict) -> dict:
        """POST /move -> an answer, an error, or `{type: 'cancelled'}`."""
        url = self.endpoints.api_url("/move")
        if not url:
            return build_error({"key": "api.unreachable", "params": {"url": ""}}, request=req)
        try:
            res = await self.http.post(url, json=move_payload(req), timeout=MOVE_TIMEOUT,
                                       headers=self.endpoints.api_headers())
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            log.info("/move unreachable at %s: %s", url, exc)
            return build_error({"key": "api.unreachable", "params": {"url": url}},
                               hint={"key": "api.unreachableHint"}, request=req)

        text = res.text
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None

        # 499: the endpoint dropped this solve because it was cancelled. Normally
        # it is never seen (our own request was abandoned first), but a lost race
        # or a cancel from another client can deliver it — and it is not an error
        # to show, it is an answer we asked not to receive.
        if res.status_code == 499:
            return {"type": "cancelled", "request": req}

        if not res.is_success:
            fallback = text or f"{res.status_code} {res.reason_phrase}"
            return build_error(detail_of(parsed, fallback),
                               payload=parsed if parsed is not None else text,
                               hint={"key": "api.refusedHint"} if res.status_code == 400 else None,
                               request=req, status=res.status_code)

        if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
            return build_error({"key": "api.notAnAnswer"},
                               payload=parsed if parsed is not None else text,
                               request=req, status=res.status_code)
        return build_answer(parsed, req)

    def cancel(self, request_id: str | None) -> None:
        """POST /cancel — kill an in-flight solve server-side.

        Abandoning the request only closes OUR end of the connection; the
        endpoint would keep solving to completion and keep holding the solve
        semaphore, so the question we actually want would queue behind an answer
        nobody will read. This is what stops the work: it SIGKILLs the
        subprocess stage and frees the lock before the replacement arrives.

        Fire-and-forget by design — a cancel that fails changes nothing we can
        act on.
        """
        url = self.endpoints.api_url("/cancel")
        if not url or not request_id:
            return

        async def send() -> None:
            try:
                await self.http.post(url, json={"requestId": request_id}, timeout=CANCEL_TIMEOUT,
                                     headers=self.endpoints.api_headers())
            except httpx.HTTPError:
                pass  # nothing useful to do about a failed cancel

        self._spawn(send())

    async def health(self) -> dict:
        """GET /health — proof the API is reachable, and its tuning defaults."""
        url = self.endpoints.api_url("/health")
        if not url:
            raise RuntimeError("no API is configured")
        res = await self.http.get(url, timeout=HEALTH_TIMEOUT, headers=self.endpoints.api_headers())
        if not res.is_success:
            raise RuntimeError(f"{res.status_code} {res.reason_phrase}")
        info = res.json()
        if not isinstance(info, dict):
            raise RuntimeError("/health did not answer with an object")
        return info

    async def screen_error(self, record: dict) -> httpx.Response:
        url = self.endpoints.api_url("/screenError")
        if not url:
            raise RuntimeError("no API is configured")
        return await self.http.post(url, json=record, timeout=SCREEN_ERROR_TIMEOUT,
                                    headers=self.endpoints.api_headers())
