"""JavaScript's number rules, where the snapshot text depends on them.

The body the host writes, and the lines this coordinator writes into it for the
operator (`VPIP=41%`, `78.9BB average stack`), were produced by JavaScript for
as long as the front end prepared the hand — and the parser on the other side
of /move has only ever seen that. Python rounds half to EVEN and prints a float
as `41.0`, so either default would change the bytes of a body that has not
changed. These three are the JS behaviour, spelled out:

  * `js_round`  — Math.round: the nearest integer, ties toward +infinity;
  * `js_str`    — String(number): no `.0` on a whole number;
  * `parse_float_prefix` — parseFloat: the longest leading number, or None
    where JS would produce NaN.
"""
from __future__ import annotations

import math
import re

_FLOAT_PREFIX = re.compile(r"\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def js_round(x: float) -> int:
    """Math.round. Exact: `x - floor(x)` has no rounding error of its own."""
    r = math.floor(x)
    return r + 1 if x - r >= 0.5 else r


def js_str(n: float) -> str:
    """String(n) for the finite numbers a snapshot carries."""
    if isinstance(n, bool):
        return "true" if n else "false"
    if n != n:
        return "NaN"
    if math.isinf(n):
        return "Infinity" if n > 0 else "-Infinity"
    if float(n).is_integer() and abs(n) < 1e21:
        return str(int(n))
    return repr(float(n))


def parse_float_prefix(text: str) -> float | None:
    """parseFloat, with None standing in for NaN."""
    m = _FLOAT_PREFIX.match(str(text))
    return float(m.group(0)) if m else None


def to_number(value) -> float | None:
    """Number(value) for what a setting or a typed field can hold, or None for NaN.

    `''` is 0 in JS, and so it is here; the callers that mean "empty is not a
    zero" test for it before they ever get this far, exactly as the JS did.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        f = float(value)
        return None if f != f else f
    text = str(value).strip()
    if not text:
        return 0.0
    if not _DECIMAL.fullmatch(text):
        return None
    return float(text)


def is_finite_number(value) -> bool:
    """Number.isFinite(Number(value))."""
    n = to_number(value)
    return n is not None and math.isfinite(n)


def tidy(n: float) -> float | int:
    """A whole float as an int, so what is stored and sent reads `781`, not `781.0`."""
    return int(n) if isinstance(n, float) and n.is_integer() and abs(n) < 2**53 else n
