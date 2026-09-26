"""The play drawn from an answer, worded for the bot host to act on.

    move(fold)  move(check)  move(call)
    move(bet 33%(4.4 BB))  move(raise 60%(27.5 BB))  move(all-in(41.3 BB))

The percent is of the pot, the BB figure the hero's street TO-amount — both as
the endpoint's own labels have them (`bet 33%(4.4BB)`). Two of its labels need
more than a rewording:

  * preflop raises carry no percent (`raise 8.4BB`), so it is worked out from
    the snapshot with the endpoint's own formula: the raise over the bet faced,
    as a share of the pot after calling;
  * `all-in` is also how the endpoint says "call": an all-in that is not a bet
    or a raise — the hero's stack is no more than the price, or it calls a
    shove the hero covers — goes as `move(call)`.
"""
from __future__ import annotations

import re

from .answers import split_action

SLACK_BB = 0.05
"""Stacks and bets are read off the screen to 0.1BB, so "no more than the price"
is judged to half of that."""


def _fmt_pct(x: float) -> str:
    """The endpoint's `_fmt_pct`: whole when it is within 0.75 of one."""
    r = round(x)
    return f"{int(r)}" if abs(x - r) < 0.75 else f"{x:.1f}"


def _fmt_bb(x: float) -> str:
    return f"{x:.1f}"


def _label_pct(action: str) -> str | None:
    """The percent exactly as the label wrote it."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", action)
    return m.group(1) if m else None


def _pot(hand: dict) -> float:
    return hand.get("pot") or hand.get("replayedPot") or 0.0


def _computed_pct(kind: str, to: float, hand: dict) -> str | None:
    hero = hand.get("hero") or {}
    pot = _pot(hand)
    to_call = hand.get("toCall") or 0.0
    if kind == "bet":
        base, over = pot, to - (hero.get("streetCommit") or 0.0)
    else:
        base, over = pot + to_call, to - (hand.get("currentBet") or 0.0)
    if base <= 0:
        return None
    return _fmt_pct(100.0 * over / base)


def _is_all_in_call(bb: float | None, hand: dict) -> bool:
    """Is this all-in only a call? Anything that is not a bet or a raise is.

    The endpoint says `all-in` for a call in two cases, and neither puts in a
    chip more than the price:
      * the hero is the short one — the call takes the last of the stack;
      * the villain shoved and the hero covers — postflop, calling an all-in is
        reported as `all-in(X)` with X the bet being called, however deep the
        hero is.
    """
    to_call = hand.get("toCall") or 0.0
    if to_call <= 0:
        return False
    hero = hand.get("hero") or {}
    stack = hero.get("stack")
    if stack is not None and stack <= to_call + SLACK_BB:
        return True
    # The label's own to-amount goes no higher than the bet being faced.
    if bb is not None and bb <= (hand.get("currentBet") or 0.0) + SLACK_BB:
        return True
    # Nobody left in the hand has a chip behind, so there is nothing to raise.
    others = [s for s in hand.get("seats") or [] if not s.get("isHero") and not s.get("folded")]
    return bool(others) and all(s.get("allIn") for s in others)


def bot_move(action: dict | None, hand: dict | None) -> str | None:
    """A drawn answer row + the snapshot it answers -> `move(...)`, or None when
    there is nothing the bot could act on."""
    if not action or not hand:
        return None
    label = str(action.get("action") or "")
    parts = split_action(label)
    kind, bb = parts["kind"], parts["bb"]
    if kind in ("fold", "check", "call"):
        return f"move({kind})"
    if kind == "all-in":
        if _is_all_in_call(bb, hand):
            return "move(call)"
        if bb is None:
            hero = hand.get("hero") or {}
            if hero.get("stack") is None:
                return None
            bb = (hero.get("streetCommit") or 0.0) + hero["stack"]
        return f"move(all-in({_fmt_bb(bb)} BB))"
    if kind in ("bet", "raise"):
        if bb is None:
            return None
        pct = _label_pct(label) or _computed_pct(kind, bb, hand)
        if pct is None:
            return None
        return f"move({kind} {pct}%({_fmt_bb(bb)} BB))"
    return None
