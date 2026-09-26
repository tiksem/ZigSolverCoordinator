"""Which question the solver is asked, and who decides.

Four choices, two of which are the endpoint's `regime` verbatim:

  gto       the equilibrium strategy at the node
  exploit   the maximum-EV action against this villain's measured behaviour
  manual    neither — ask once per hand, and send whichever was picked
  advanced  neither — draw one per hand at a mix the operator set

`manual` and `advanced` never reach the API. They are modes that decide which
of `gto` / `exploit` goes out: manual by asking, advanced by rolling.

Both decide it ON THE FLOP, ONCE, FOR THE WHOLE HAND. A hand is one line and the
two regimes answer different questions about it, so switching between them on
the turn produces a line neither of them would have played — the flop bet sized
by an equilibrium the turn then abandons, or an exploit that never gets to
collect.

PREFLOP IS MOSTLY NOT PART OF THIS. Manual and advanced leave it to the preflop
engine: nothing is asked or drawn for it. The one preflop question the exploit
regime does answer is the ALL-IN CALCULATOR (api/preflop_allin.py) — where the
effective stack is short enough for a shove to be the move to price, or a shove
is already in front of the hero — so with Exploit selected such a spot goes out
as `exploit`; a deeper preflop spot is played by the preflop engine and is not
a spot the regime fell back from.
"""
from __future__ import annotations

import random
from typing import Any

from .hand_body import is_known_stat
from .jsnum import is_finite_number, js_round, to_number

MIN_STATS_FOR_EXPLOIT = 3
"""How many HUD stats the villain must carry before the advanced regime may draw
Exploit, when that gate is on.

Three is the point where the read stops being the population average: the
exploit models impute every stat the HUD does not carry, so one or two of them
describe the average player with a small dent in it, not this villain."""

REGIMES = ["gto", "exploit", "manual", "advanced"]

PREFLOP_ALLIN_MAX_EFF_BB = 30.0
"""Preflop, the exploit regime is the all-in calculator, which applies where the
effective stack — the hero's total against the raiser being faced, else against
the biggest live opponent — is at most this, or a live opponent is all-in.
Mirrors api/constants.PF_ALLIN_MAX_EFF_BB, so the chip and the regime bar can
say a deep preflop spot is the chart's before the endpoint says so."""


def _clamp_pct(value: Any) -> int:
    return min(100, max(0, js_round(to_number(value))))


class RegimeState:
    def __init__(self, raw: Any = None) -> None:
        self.selected = "gto"
        # Advanced: the weight on Exploit, in percent. 0 = always GTO, 100 =
        # always Exploit — both ends are legal, and are how you park the mode.
        self.exploit_pct = 50
        # Advanced: refuse to draw Exploit against a villain the HUD barely
        # covers. On by default — an exploit answer with no read is the
        # population average dressed as a read, which is the one thing GTO does
        # better.
        self.require_stats = True
        if raw is None:
            return
        # Before the advanced regime this was stored as the bare regime string.
        saved = raw if isinstance(raw, dict) else {"selected": raw}
        if saved.get("selected") in REGIMES:
            self.selected = saved["selected"]
        if saved.get("exploitPct") is not None and is_finite_number(saved["exploitPct"]):
            self.exploit_pct = _clamp_pct(saved["exploitPct"])
        if isinstance(saved.get("requireStats"), bool):
            self.require_stats = saved["requireStats"]

    def select(self, value: Any) -> bool:
        if value not in REGIMES:
            return False
        changed = value != self.selected
        self.selected = value
        return changed

    def set_exploit_pct(self, value: Any) -> bool:
        if not is_finite_number(value) or value is None or value == "":
            return False
        before = self.exploit_pct
        self.exploit_pct = _clamp_pct(value)
        return before != self.exploit_pct

    def set_require_stats(self, on: Any) -> bool:
        before = self.require_stats
        self.require_stats = bool(on)
        return before != self.require_stats

    def flip_coin(self) -> str:
        """The advanced regime's coin. Drawn ONCE PER HAND, ON THE FLOP — the
        caller is what keeps it, and what makes sure it is not drawn before
        there is a flop.

        Deliberately knows nothing about the spot: the coin is the hand's
        question, and whether the hand can carry it is `resolve_advanced`."""
        return "exploit" if random.random() * 100 < self.exploit_pct else "gto"

    def to_wire(self) -> dict:
        return {"selected": self.selected, "exploitPct": self.exploit_pct,
                "requireStats": self.require_stats}


def _seats_to_the_flop(hand: dict) -> list[dict]:
    return [s for s in hand["seats"]
            if not any(a["street"] == 0 and a["kind"] == "fold" for a in s["actions"])]


def exploit_availability(hand: dict | None) -> dict:
    """Can the exploit regime answer THIS hand?

    THREE answers, not two, because "not yet" and "no" are different things to
    tell an operator:

      ok       heads-up postflop — exploit answers this hand
      pending  nothing to decide yet: no snapshot, or preflop. NOT a refusal,
               and never worth a warning — the regime is decided on the flop.
      no       this hand is GTO whatever is selected, and `why` is what did it

    The refusals mirror api/exploit_spot.py's, so the manual prompt is not
    raised for a hand whose answer would come back GTO whatever was picked.

    Heads-up ON THE FLOP is not the same as heads-up now: a pot dealt three ways
    is a three-way game even after someone folds, and the models were never
    fitted on one. Both refusals are properties of the hand rather than of the
    street, which is what lets one verdict on the flop stand for the whole hand.

    `why` is a `{key, count?}` pair, not a sentence — the front end words it.
    """
    if not hand:
        return {"status": "pending", "ok": False, "why": None}
    if not hand["street"]:
        # Preflop: the all-in calculator, where it applies (any table size --
        # it prices heads-up too); otherwise the preflop engine's spot, which
        # is "not yet" rather than a refusal, with the depth as the reason.
        applies, eff = preflop_allin_applies(hand)
        if applies:
            return {"status": "ok", "ok": True, "why": None}
        return {"status": "pending", "ok": False,
                "why": {"key": "reason.deepPreflop", "count": int(round(eff))}}
    if hand["tableSize"] < 3:
        return {"status": "no", "ok": False, "why": {"key": "reason.headsUpTable"}}
    saw_flop = _seats_to_the_flop(hand)
    if len(saw_flop) != 2:
        return {"status": "no", "ok": False,
                "why": {"key": "reason.multiwayFlop", "count": len(saw_flop)}}
    return {"status": "ok", "ok": True, "why": None}


def _total(seat: dict) -> float:
    return (seat.get("stack") or 0) + (seat.get("streetCommit") or 0)


def preflop_allin_applies(hand: dict) -> tuple[bool, float]:
    """(does the all-in calculator apply to this preflop decision, the effective
    stack it measured) -- the same rule as api/preflop_allin.applies."""
    hero = hand.get("hero")
    live = [s for s in hand["seats"] if not s["folded"] and not s["isHero"]]
    if not hero or not live:
        return False, 0.0
    to_call = hand.get("toCall") or 0
    hero_stack = hero.get("stack") or 0
    facing_allin = to_call > 0 and (any(s["allIn"] for s in live) or to_call >= hero_stack - 1e-9)
    raisers = [s for s in live
               if s.get("lastAction") and s["lastAction"].get("kind") in ("raise", "all-in", "bet")]
    if raisers:
        aggr = max(raisers, key=lambda s: s.get("streetCommit") or 0)
        eff = min(_total(hero), _total(aggr))
    else:
        eff = min(_total(hero), max(_total(s) for s in live))
    return facing_allin or eff <= PREFLOP_ALLIN_MAX_EFF_BB, eff


def villain_read(hand: dict | None) -> dict:
    """What the HUD carries on the villain of this spot.

    Only stats the endpoint actually reads are counted: a key the models have no
    coefficient for is not a read, whatever the client put in the body. The
    villain is the seat opposite the hero among those still in from the flop —
    where there is no single one (preflop, multiway) the count is 0.
    """
    none = {"seat": None, "stats": [], "count": 0}
    if not hand or not hand["hero"]:
        return none
    others = [s for s in _seats_to_the_flop(hand) if not s["isHero"]]
    if len(others) != 1:
        return none
    seat = others[0]
    stats = [k for k in (seat.get("stats") or {}) if is_known_stat(k)]
    return {"seat": seat, "stats": stats, "count": len(stats)}


def thin_read_reason(count: int) -> dict:
    """"The HUD barely covers this villain", as a quotable reason.

    Zero is its own key rather than the plural's zero form: "no stats" and
    "only 0 stats" are the same fact and only one of them is a sentence."""
    return {"key": "reason.noStats"} if count == 0 else {"key": "reason.fewStats", "count": count}


def resolve_advanced(hand: dict | None, coin: str, require_stats: bool) -> dict:
    """This hand's coin, resolved against the hand.

    `forced` is set only where the refusal actually cost something — the coin
    came up Exploit and this hand cannot carry it — so the front end can report
    the hand's draw without pretending a refusal was a GTO flip. A `pending`
    spot is not a refusal and never sets it.
    """
    if coin != "exploit":
        return {"coin": "gto", "regime": "gto", "forced": None}
    availability = exploit_availability(hand)
    if availability["status"] != "ok":
        return {"coin": coin, "regime": "gto", "forced": availability["why"]}
    if require_stats:
        count = villain_read(hand)["count"]
        if count < MIN_STATS_FOR_EXPLOIT:
            return {"coin": coin, "regime": "gto", "forced": thin_read_reason(count)}
    return {"coin": coin, "regime": "exploit", "forced": None}
