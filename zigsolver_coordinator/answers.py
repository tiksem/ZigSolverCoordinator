"""Normalizes a /move answer into what the front end's panel renders.

    { actions: [{action, probability, evBB?, evPot?, support?, evICM?, pWin?}],
      regime, regimeRequested, solver, street, hand, responseTime,
      meta: { flow, cached, board, actingSeat, seats, potBB, toCallBB,
              solveSeconds, handDecisions, warnings, ranges, ... } }

ONE list, because the endpoint answers one question per call. A GTO answer is a
distribution to mix at; an exploit answer is an argmax, so `probability` is 1 on
one row and 0 on the rest and the EV columns are what actually decided it.
`regime` is what ran and `regimeRequested` what was asked for — they differ
whenever the exploit regime declined the spot.

Action keys are `check`, `fold`, `call(NN.NBB)`, `bet 33%(4.4BB)`,
`raise 60%(27.5BB)`, `all-in(37.8BB)` postflop, heads-up and multiway alike.
Preflop still carries the BB-only `raise 8.4BB` style, since ranges there aren't
anchored to a pot.

The draw lives here too — the move you would actually make, sampled from a GTO
distribution — so every front end watching a table reads the SAME pick. When
each page drew its own, two tabs on one table could show two different plays.
"""
from __future__ import annotations

import itertools
import random
import re
import time
from typing import Any

from .jsnum import to_number

_KIND_ORDER = {"fold": 0, "check": 1, "call": 2, "bet": 3, "raise": 4, "all-in": 5}

_ids = itertools.count(1)


def next_id() -> int:
    """A result id, unique for the life of this process."""
    return next(_ids)


def now_ms() -> int:
    return int(time.time() * 1000)


def action_kind(key: Any) -> str:
    k = str(key).lower()
    for prefix, kind in (("fold", "fold"), ("check", "check"), ("call", "call"),
                         ("bet", "bet"), ("raise", "raise"), ("all", "all-in")):
        if k.startswith(prefix):
            return kind
    return "other"


def split_action(key: Any) -> dict:
    """`bet 33%(4.4BB)` -> {kind: 'bet', pct: 33, bb: 4.4}."""
    text = str(key)
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    bb = re.search(r"\(?\s*(\d+(?:\.\d+)?)\s*BB\s*\)?", text, re.I)
    return {
        "kind": action_kind(text),
        "pct": float(pct.group(1)) if pct else None,
        "bb": float(bb.group(1)) if bb else None,
    }


def _probability(value: Any) -> float:
    """Number(p) || 0."""
    n = to_number(value) if value is not None else 0.0
    return n or 0.0


def _to_list(actions: Any) -> list[dict]:
    if not isinstance(actions, list):
        return []
    out = []
    for a in actions:
        if not isinstance(a, dict) or a.get("action") is None:
            continue
        out.append({
            "action": a["action"],
            "probability": _probability(a.get("probability")),
            # Exploit rows only. None rather than falsy-to-None: an EV of exactly
            # 0 is the fold branch, which is the reference every other number is
            # read against and the last one to drop.
            "evBB": a.get("evBB"),
            "evPot": a.get("evPot"),
            "support": a.get("support"),
            # The preflop all-in calculator's rows (solver "preflop-allin", and
            # the continue rows of "chart+allin"): the same EV in prize equity
            # (None without a tournament) and how often the hero takes the pot.
            "evICM": a.get("evICM"),
            "pWin": a.get("pWin"),
            "pFoldThrough": a.get("pFoldThrough"),
            "showdownEquity": a.get("showdownEquity"),
            **split_action(a["action"]),
        })
    return out


def _sort_for_display(rows: list[dict]) -> list[dict]:
    def size(a: dict) -> float:
        if a["bb"] is not None:
            return a["bb"]
        return a["pct"] if a["pct"] is not None else 0

    return sorted(rows, key=lambda a: (_KIND_ORDER.get(a["kind"], 9), size(a)))


def sample_action(rows: list[dict]) -> dict | None:
    """One draw from a probability distribution (the move you would actually make)."""
    total = sum(a["probability"] for a in rows)
    if total <= 0:
        return None
    r = random.random() * total
    for a in rows:
        r -= a["probability"]
        if r <= 0:
            return a
    return rows[-1]


def build_answer(payload: dict, request: dict | None = None) -> dict:
    """A /move response body -> a render-ready answer.

    `request` is what we asked for, so the panel can say what was requested even
    when the endpoint answered a different regime.
    """
    request = request or {}
    is_exploit = payload.get("regime") == "exploit"
    raw = _to_list(payload.get("actions"))
    # An exploit answer is ranked by EV and the ordering IS the recommendation,
    # so it is left alone. A GTO distribution has no order of its own and reads
    # best grouped fold/check/call/bet/raise.
    actions = raw if is_exploit else _sort_for_display(raw)
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    regime = payload.get("regime") or "gto"
    requested = payload.get("regimeRequested") or regime
    return {
        "id": next_id(),
        "receivedAt": now_ms(),
        "type": "answer",
        "payload": payload,
        "request": request,
        "street": payload.get("street"),
        "hand": payload.get("hand"),
        "solver": payload.get("solver"),
        "regime": regime,
        "regimeRequested": requested,
        # The endpoint declined the question and answered the other one. Not an
        # error — the table still needs an action — but the panel must not let
        # it pass for what was asked.
        "fellBack": requested != regime,
        "responseTime": payload.get("responseTime"),
        "actions": actions,
        # An argmax has nothing to sample: the top row IS the move. A
        # distribution does, and the draw is the point of showing one.
        "sampled": (actions[0] if actions else None) if is_exploit else sample_action(actions),
        "meta": meta,
        "warnings": meta.get("warnings") or [],
    }


def build_error(message: Any, *, payload: Any = None, hint: Any = None,
                request: dict | None = None, status: int | None = None) -> dict:
    """A refused or unreachable call. `hint` is the actionable half — a 400 from
    the endpoint explains itself, a network failure does not.

    Both may be a plain string (what the endpoint said, in its own words) or a
    `{key, params}` pair for one of ours, which the front end words in whatever
    language is selected when it is drawn.
    """
    return {
        "id": next_id(),
        "receivedAt": now_ms(),
        "type": "error",
        "message": message,
        "hint": hint,
        "payload": payload,
        "request": request or {},
        # The HTTP status, or None when the call never reached the endpoint. Not
        # rendered — it goes into the screen capture's record, where "refused
        # with 400" and "never arrived" are different bugs.
        "status": status,
    }
