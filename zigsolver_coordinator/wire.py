"""JSON for the wire and for the data directory.

One rule on top of `json.dumps`: nothing that is not JSON leaves this process.
Python writes NaN and Infinity by default, which JSON.parse rejects outright —
one NaN anywhere in an answer (an API that emitted one, a division that made
one) would make the whole message unreadable to the front end. JavaScript's own
JSON.stringify writes them as null, so that is what they become here.
"""
from __future__ import annotations

import json
import math
from typing import Any


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        return json.dumps(_finite(value), ensure_ascii=False, separators=(",", ":"))


def copy(value: Any) -> Any:
    """A deep copy that is guaranteed to be JSON — what the history keeps."""
    return json.loads(dumps(value))
