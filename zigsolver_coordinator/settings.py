"""Everything about how a solve is requested, in one place and persisted.

These map onto the /move request body (api/move.MoveRequest); the comments are
what each one actually does at the endpoint, because they are what the front
end's settings sheet shows.
"""
from __future__ import annotations

import math
from typing import Any

from .jsnum import tidy, to_number

DEFAULTS: dict[str, Any] = {
    # `maxSolveTime` — wall-time budget for the flop solve, in seconds. The
    # balancer picks the strongest regime that fits it.
    "maxSolveTime": 15,
    # `maxNetSolveTime` — seconds. The budget of the net solve ITSELF (its CFR
    # iterations; range generation, tree build and setup come on top) when the
    # balancer settles on the net-truncated flow; the ladder itself is still
    # priced against `maxSolveTime`. None = the net spends `maxSolveTime`.
    "maxNetSolveTime": None,
    # Send each snapshot to the API as it arrives. Off = only Re-solve and a
    # change of regime call the solver.
    "autoSolve": True,
    # `handId` — keys the per-hand tree cache, so a later street and a re-solve
    # under a different read reuse the tree instead of solving again.
    "useHandCache": True,
    # `statHands` — how many hands the body's HUD stats were measured over. It
    # prices the read the exploit regime runs on: everything the HUD does not
    # carry is imputed from the population, and a thin sample widens those
    # imputations. None = unknown, read as "a lot".
    "statHands": None,
    # Cancel the running solve when a newer snapshot arrives (POST /cancel).
    "cancelSuperseded": True,
    # Keep the sampled action stable while the same spot is re-solved, instead
    # of drawing a fresh one from every answer. GTO only — an exploit answer is
    # an argmax and has no draw to hold.
    "stableSample": False,
    # --- flop solve quality (None = whatever the API's own default is) -------
    # All FLOP ONLY: turn and river are solved as their own street at the widest
    # sizing grid, with no ladder and no floor. The API publishes its defaults
    # and accepted ranges on /health.solveTuning.
    #
    # `gateExploitability` — % of pot. An exact regime is preferred to the
    # net-truncated flow when its PREDICTED exploitability is at or under this.
    # 0 = never solve exactly.
    "gateExploitability": None,
    # `targetExploitability` — % of pot. The solve stops once its own
    # best-response check reaches this.
    "targetExploitability": None,
    # `minSolveTime` — seconds. Floor under that early stop; it never adds
    # iterations beyond the budget's cap.
    "minSolveTime": None,
}

LIMITS: dict[str, tuple[float, float]] = {
    "maxSolveTime": (0.5, 600),
    "maxNetSolveTime": (0.5, 600),
    "statHands": (1, 1_000_000),
    "gateExploitability": (0, 100),
    "targetExploitability": (0.01, 100),
    "minSolveTime": (0, 600),
}

BUDGET_PRESETS = [5, 10, 15, 30, 60, 120]
GATE_PRESETS = [1, 2, 3, 5, 10]

# The request fields a solve carries straight from these settings.
REQUEST_FIELDS = ["maxSolveTime", "maxNetSolveTime", "statHands",
                  "gateExploitability", "targetExploitability", "minSolveTime"]


def clamp_number(key: str, value: Any) -> float | int | None:
    n = to_number(value)
    if n is None or not math.isfinite(n):
        return None
    limit = LIMITS.get(key)
    if limit:
        n = min(limit[1], max(limit[0], n))
    return tidy(n)


class Settings:
    def __init__(self, raw: Any = None) -> None:
        self.values: dict[str, Any] = dict(DEFAULTS)
        if isinstance(raw, dict):
            for key, default in DEFAULTS.items():
                if raw.get(key) is None:
                    continue
                if isinstance(default, bool):
                    self.values[key] = bool(raw[key])
                    continue
                # Numeric keys are re-clamped on the way in: the limits move
                # between builds, and a stored value outside the current ones is
                # not a setting.
                n = clamp_number(key, raw[key])
                if n is not None:
                    self.values[key] = n

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def set(self, key: str, value: Any) -> bool:
        """Set one setting; True when it changed."""
        if key not in DEFAULTS:
            return False
        before = self.values[key]
        default = DEFAULTS[key]
        if isinstance(default, bool):
            self.values[key] = bool(value)
        elif value is None or value == "":
            self.values[key] = default
        else:
            self.values[key] = clamp_number(key, value)
        return self.values[key] != before

    def reset(self) -> bool:
        changed = self.values != DEFAULTS
        self.values = dict(DEFAULTS)
        return changed

    def to_wire(self) -> dict:
        return dict(self.values)

    @staticmethod
    def meta() -> dict:
        """What the front end's sheet needs besides the values: the defaults
        (to know what "Reset" would change), the limits and the presets."""
        return {
            "defaults": dict(DEFAULTS),
            "limits": {k: list(v) for k, v in LIMITS.items()},
            "budgetPresets": BUDGET_PRESETS,
            "gatePresets": GATE_PRESETS,
        }
