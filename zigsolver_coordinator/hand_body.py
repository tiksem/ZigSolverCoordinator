"""The /move `body` — the poker client's textual table snapshot.

It is exactly what the bot host's mode=0 socket pushes for each decision, and
this module both READS it (for the felt, the exploit gate and every decision
about whether a snapshot is worth a solve) and WRITES into it (the HUD stats and
the tournament header the operator typed, in the host's own format).

Mirrors api/handhistory.py of ZigSolver: an optional tournament header, then one
block per action in acting order, with `Board: 4♥ T♦ 8♣` lines separating the
streets (each carrying the full board so far).

    K Barsukov          <- player name; the hero is "*me*"
    position=CO         <- UTG UTG+1 UTG+2 LJ MP HJ CO BTN SB BB
    call                <- fold|check|call|bet 4.1BB|raise 2.0BB|all in 41.3BB
    VPIP=25%            <- HUD stats, kept verbatim
    stack=147.8BB       <- chips BEHIND right now

The Python side of /move is strict about sequence (it has to solve the spot);
here we only need to *render* it and reason about it, so parsing is deliberately
forgiving — anything unreadable lands in `warnings` and the rest still draws.

Those warnings are `{key, params}` pairs rather than sentences: a snapshot parsed
an hour ago should read in the language the front end has selected NOW, so the
sentence is built where it is shown.

A port of the front end's src/lib/handBody.js, which prepared the hand before
this coordinator existed. What it returns is a WIRE object — camelCase, shaped
the way the front end's components read it — and tests/fixtures holds the JS
implementation's own output for the same bodies, so the two cannot drift apart
unnoticed.
"""
from __future__ import annotations

import re
from typing import Any

from .jsnum import is_finite_number, js_round, js_str, parse_float_prefix, to_number

POSITION_ORDER = ["UTG", "UTG+1", "UTG+2", "LJ", "MP", "HJ", "CO", "BTN", "SB", "BB"]

HERO_NAME = "*me*"

_POS_CANON = {p: p for p in POSITION_ORDER}
_POS_CANON["UTG1"] = "UTG+1"
_POS_CANON["UTG2"] = "UTG+2"

_SUIT_SYM = {
    "♠": "s", "♥": "h", "♦": "d", "♣": "c",
    "s": "s", "h": "h", "d": "d", "c": "c", "S": "s", "H": "h", "D": "d", "C": "c",
}

_RANKS = "23456789TJQKA"

_ACT_RE = re.compile(
    r"^(fold|check|call|raise|bet|all[\s-]?in|allin|waiting|wait)\b\s*([\d.]*)\s*(?:bb)?\s*$",
    re.I,
)

STREET_NAMES = ["preflop", "flop", "turn", "river"]

# The line the host appends when the hand is OVER instead of asking anything.
#
# It is not a player block — it has no `position=` — and the endpoint says so:
# `line 67: block for 'Hand finished' has no position=`. /move only answers a
# body that ends on the hero's decision, so a snapshot carrying this marker is
# never worth sending; `parse_hand_body` flags it (`finished`) and the table
# stops short of the API rather than spending a request on a refusal.
_HAND_OVER_RE = re.compile(r"^hand\s*(?:is\s*)?(?:finished|finish|over|ended|complete[d]?)\b", re.I)

_POSITION_LINE = re.compile(r"position\s*=", re.I)
_BODY_RE = re.compile(r"^\s*position\s*=", re.I | re.M)


def is_hand_over_line(text: Any) -> bool:
    """Is this line the host's "the hand is over" marker rather than a block?"""
    return bool(_HAND_OVER_RE.match(str(text or "").strip()))


def looks_like_body(text: Any) -> bool:
    """Does this socket frame look like a table snapshot rather than a log line?"""
    return bool(_BODY_RE.search(text or ""))


def _norm_card(tok: Any) -> str | None:
    t = str(tok).strip().replace("10", "T", 1)
    if len(t) != 2:
        return None
    suit = _SUIT_SYM.get(t[1])
    rank = t[0].upper()
    if not suit or len(rank) != 1 or rank not in _RANKS:
        return None
    return rank + suit


def norm_hand(text: Any) -> list[str] | None:
    """'Q♠J♠' / 'Q♠ J♠' / 'QsJs' -> ['Qs', 'Js'] (or None)."""
    t = str(text).strip().replace("10", "T")
    t = "".join(_SUIT_SYM.get(ch, ch) for ch in t)
    t = re.sub(r"[\s,]+", "", t)
    if len(t) != 4:
        return None
    a = _norm_card(t[:2])
    b = _norm_card(t[2:])
    if not a or not b or a == b:
        return None
    return [a, b]


def _canon_pos(text: Any) -> str | None:
    return _POS_CANON.get(re.sub(r"\s", "", str(text).strip().upper()))


def _num(text: Any) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_action(line: str) -> dict | None:
    m = _ACT_RE.match(line.strip())
    if not m:
        return None
    kind = re.sub(r"[\s-]", "", m.group(1).lower())
    if kind == "allin":
        kind = "all-in"
    elif kind in ("waiting", "wait"):
        kind = None
    return {"kind": kind, "amount": parse_float_prefix(m.group(2)) if m.group(2) else None}


def _parse_header(text: str) -> dict:
    def grab(pattern: str) -> float | None:
        m = re.search(pattern, text, re.I)
        return float(m.group(1).replace(",", "")) if m else None

    players_left = grab(r"(\d[\d,]*)\s*players?\s+left")
    players_paid = grab(r"(\d[\d,]*)\s*players?\s+paid")
    average_stack = grab(r"(\d+(?:\.\d+)?)\s*BB\s+average")
    if average_stack is None:
        average_stack = grab(r"average\s+stack[^\d]*(\d+(?:\.\d+)?)")
    total_pot = grab(r"total\s+pot[^\d]*(\d+(?:\.\d+)?)")
    # Any ONE of the three is worth showing: clients word the header differently
    # and a missing "players paid" should not hide the average stack.
    tournament = (
        {"playersLeft": players_left, "playersPaid": players_paid, "averageStack": average_stack}
        if players_left is not None or players_paid is not None or average_stack is not None
        else None
    )
    return {"tournament": tournament, "totalPot": total_pot, "text": text.strip()}


def _parse_block(lines: list[dict], warnings: list) -> dict:
    name = lines[0]["text"]
    block = {
        "name": name,
        "isHero": name.strip() == HERO_NAME,
        "position": None,
        "stack": None,
        "stats": {},
        "hand": None,
        "kind": None,
        "amount": None,
        "waiting": True,
    }
    have_action = False
    for line in lines[1:]:
        no, text = line["no"], line["text"]
        eq = text.find("=")
        if eq > 0:
            key = text[:eq].strip().upper()
            val = text[eq + 1:]
            if key == "POSITION":
                pos = _canon_pos(val)
                if pos:
                    block["position"] = pos
                else:
                    warnings.append({"key": "parse.unknownPosition",
                                     "params": {"line": no, "value": val.strip()}})
            elif key == "STACK":
                block["stack"] = _num(val)
            elif key in ("HAND", "CARDS", "HOLECARDS", "HOLE CARDS"):
                block["hand"] = norm_hand(val)
            else:
                v = _num(val)
                if v is not None:
                    block["stats"][key] = v
                else:
                    warnings.append({"key": "parse.unreadableStat", "params": {"line": no, "text": text}})
            continue
        act = _parse_action(text)
        if act:
            if have_action:
                warnings.append({"key": "parse.secondAction",
                                 "params": {"line": no, "text": text, "name": name}})
                continue
            block["kind"] = act["kind"]
            block["amount"] = act["amount"]
            block["waiting"] = act["kind"] is None
            have_action = True
            continue
        hand = norm_hand(text)
        if hand:
            block["hand"] = hand
        else:
            warnings.append({"key": "parse.unrecognizedLine", "params": {"line": no, "text": text}})
    return block


def _is_board_line(trimmed: str) -> bool:
    low = trimmed.lower()
    return low.startswith("board:") or low.startswith("board ")


def _split_body(body: str, warnings: list) -> tuple[str, list[dict], bool]:
    """Body text -> (header, events [{type: 'act'|'board', ...}], finished)."""
    events: list[dict] = []
    header_lines: list[str] = []
    block: list[dict] = []
    saw_player = False
    finished = False

    def flush() -> None:
        nonlocal block, saw_player
        if not block:
            return
        has_position = any(_POSITION_LINE.search(item["text"]) for item in block)
        if not saw_player and not has_position:
            header_lines.extend(item["text"] for item in block)
            block = []
            return
        events.append({"type": "act", **_parse_block(block, warnings)})
        saw_player = True
        block = []

    for i, raw in enumerate(re.split(r"\r?\n", body)):
        text = raw.strip()
        no = i + 1
        if not text:
            flush()
            continue
        if is_hand_over_line(text):
            # Dropped rather than parsed: as a block of its own it would become
            # a seat named "Hand finished", and glued to the last player's block
            # it would be an unrecognized line. Either way it is the hand ending,
            # which is the one thing the whole snapshot now means.
            finished = True
            continue
        if _is_board_line(text):
            flush()
            toks = text.split(":", 1)[1] if ":" in text else text[5:]
            cards = [c for c in (_norm_card(t) for t in toks.replace(",", " ").split()) if c]
            events.append({"type": "board", "cards": cards})
            continue
        block.append({"no": no, "text": text})
    flush()
    return "\n".join(header_lines), events, finished


def _set_at(items: list, index: int, value: Any) -> None:
    """`items[index] = value` the way a JS array takes it: growing, holes as None."""
    while len(items) <= index:
        items.append(None)
    items[index] = value


def parse_hand_body(body: Any) -> dict | None:
    """Full snapshot -> a render-ready table state, or None for a plain log line."""
    if not looks_like_body(body):
        return None
    body = str(body)
    warnings: list = []
    header, events, finished = _split_body(body, warnings)
    head = _parse_header(header)

    # --- seats, in the order the body first mentions them --------------------
    seats: dict[str, dict] = {}

    def seat_of(b: dict) -> dict:
        s = seats.get(b["name"])
        if s is None:
            s = {
                "name": b["name"],
                "isHero": b["isHero"],
                "position": b["position"],
                "order": POSITION_ORDER.index(b["position"]) if b["position"] in POSITION_ORDER else -1,
                "stack": None,
                "stats": {},
                "hand": None,
                "folded": False,
                "allIn": False,
                "streetCommit": 0,
                "totalCommit": 0,
                "lastAction": None,
                "actions": [],
                "toAct": False,
                "blindPosted": False,
            }
            seats[b["name"]] = s
        return s

    # --- replay, street by street --------------------------------------------
    street = 0
    board: list[str] = []
    board_by_street: list = [[]]
    # Preflop opens at the big blind even before the BB's own block shows up, so
    # a limp ("call" with no amount) commits 1BB rather than nothing.
    current_bet: float = 1
    pot: float = 0
    street_log: list[list[dict]] = [[], [], [], []]

    def open_street() -> None:
        nonlocal pot, current_bet
        for s in seats.values():
            pot += s["streetCommit"]
            s["streetCommit"] = 0
        current_bet = 0

    # Blinds are auto-posted by the solver's replay; the body never renders them.
    # Posted per seat as it first appears — the seat map is empty when the first
    # block is read, so there is no one moment to post them all.
    def post_blind(s: dict) -> None:
        if s["blindPosted"] or street != 0:
            return
        s["blindPosted"] = True
        if s["position"] == "SB":
            s["streetCommit"] = max(s["streetCommit"], 0.5)
        elif s["position"] == "BB":
            s["streetCommit"] = max(s["streetCommit"], 1)

    for ev in events:
        if ev["type"] == "board":
            board = ev["cards"]
            street = min(3, max(1, len(board) - 2))
            _set_at(board_by_street, street, list(ev["cards"]))
            open_street()
            continue
        s = seat_of(ev)
        if ev["position"]:
            s["position"] = ev["position"]
            s["order"] = POSITION_ORDER.index(ev["position"])
        if ev["stack"] is not None:
            s["stack"] = ev["stack"]
        if ev["hand"]:
            s["hand"] = ev["hand"]
        if ev["stats"]:
            s["stats"] = {**s["stats"], **ev["stats"]}
        post_blind(s)

        if ev["waiting"]:
            s["toAct"] = True
            continue
        s["toAct"] = False

        entry = {"street": street, "kind": ev["kind"], "amount": ev["amount"], "name": s["name"]}
        kind, amount = ev["kind"], ev["amount"]
        if kind == "fold":
            s["folded"] = True
        elif kind == "call":
            s["streetCommit"] = amount if amount is not None else current_bet
        elif kind in ("bet", "raise"):
            if amount is not None:
                s["streetCommit"] = amount
                current_bet = max(current_bet, amount)
        elif kind == "all-in":
            s["allIn"] = True
            if amount is not None:
                s["streetCommit"] = amount
                current_bet = max(current_bet, amount)
            elif s["stack"] is not None:
                s["streetCommit"] += s["stack"]
                current_bet = max(current_bet, s["streetCommit"])
        entry["commit"] = s["streetCommit"]
        s["lastAction"] = entry
        s["actions"].append(entry)
        street_log[street].append(entry)

    seat_list = list(seats.values())
    for s in seat_list:
        s["totalCommit"] = max([0, *(a.get("commit") or 0 for a in s["actions"])])
        # What a client shows next to a seat is its action on the street being
        # played, not whatever it did three streets ago.
        s["lastAction"] = next((a for a in reversed(s["actions"]) if a["street"] == street), None)
        s["streetActions"] = [a for a in s["actions"] if a["street"] == street]

    # Nobody is on the clock on a hand that is over — including whoever the body
    # still marks as waiting, since the marker came in after that block.
    if finished:
        for s in seat_list:
            s["toAct"] = False

    # The hero block may close the body with no action line ("your turn"); if
    # the client did not mark anyone waiting, the hero is still who we answer for.
    hero = next((s for s in seat_list if s["isHero"]), None)
    waiting = [s for s in seat_list if s["toAct"]]
    hero_to_act = (not finished and hero is not None
                   and (hero["toAct"] or (not waiting and not hero["lastAction"])))

    replayed_pot = pot + _sum_in_order(s["streetCommit"] or 0 for s in seat_list)

    contenders = [s for s in seat_list if not s["folded"]]
    to_call = max(0, current_bet - ((hero["streetCommit"] or 0) if hero else 0))

    by_order = sorted(seat_list, key=lambda s: s["order"])
    # The effective button: the last non-blind seat dealt in — or, heads-up (no
    # non-blind seats at all), the SB, who deals.
    non_blind = [s for s in by_order if s["position"] not in ("SB", "BB")]
    if non_blind:
        button_name = non_blind[-1]["name"]
    else:
        button_name = next((s["name"] for s in by_order if s["position"] == "SB"), None)

    total_pot = head["totalPot"]
    return {
        "kind": "snapshot",
        "raw": body,
        # The hand is over: this body is a result, not a question. Nothing solves it.
        "finished": finished,
        "headerText": head["text"],
        "tournament": head["tournament"],
        "totalPot": total_pot,
        "replayedPot": replayed_pot,
        "pot": total_pot if total_pot is not None else replayed_pot,
        "street": street,
        "streetName": STREET_NAMES[street],
        "board": board,
        "boardByStreet": board_by_street,
        "currentBet": current_bet,
        "toCall": to_call,
        "seats": by_order,
        "seatsInActionOrder": seat_list,
        "hero": hero,
        "heroHand": hero["hand"] if hero else None,
        "heroToAct": hero_to_act,
        "waitingOn": waiting[-1]["name"] if waiting else None,
        "buttonName": button_name,
        "tableSize": len(seat_list),
        "contenders": len(contenders),
        "streetLog": street_log,
        "warnings": warnings,
    }


def _sum_in_order(values) -> float:
    """reduce((a, b) => a + b, 0): left to right, so the float sum is the JS one."""
    total: float = 0
    for v in values:
        total = total + v
    return total


def decision_key(parsed: dict | None) -> str | None:
    """Which DECISION a snapshot is asking about, as a string two snapshots can be
    compared on: the street being played, the hero's cards, and how many actions
    the body has replayed to get here.

    Coarser than the body on purpose. The host re-reads the table on a timer and
    two reads of one decision routinely differ in bytes without differing in
    question — a name comes back with a different capital, a stack loses a digit,
    a HUD block appears. None of that is an action, and it is an ACTION that makes
    the spot a new one. So two snapshots with the same key are the same question
    however far their text has drifted apart, which is what lets a re-read be held
    against the answer already being computed rather than replacing it (see the
    table's shielded solve).

    None when the body has no hero cards to key on — nothing to compare, so
    nothing is treated as a repeat of it.
    """
    if not parsed or parsed["finished"] or not parsed["heroHand"]:
        return None
    acted = sum(len(s) for s in parsed["streetLog"])
    # Sorted: the two cards are one holding whichever order the client printed
    # them in, and a read that swaps them is not a different question.
    held = "".join(sorted(parsed["heroHand"]))
    return f"{parsed['street']}|{held}|{acted}"


# --- the stats -----------------------------------------------------------------

CORE_STATS = ["VPIP", "PFR", "3BET", "ATS"]
"""Stat keys the endpoint actually reads, in the order worth showing first."""

STAT_KEYS = [
    "VPIP", "PFR", "3BET", "ATS", "F3B", "FTS BB", "FTS SB", "W$SD", "WTSD", "WWSF", "AF",
    "FLOP C-BET", "TURN C-BET", "RIVER C-BET",
    "FLOP FOLD TO C-BET", "TURN FOLD TO C-BET", "RIVER FOLD TO C-BET",
    "ALL-IN FREQUENCY",
]
"""Every stat key the endpoint has a coefficient for.

This list decides whether a HUD value counts as a read (regime.villain_read),
and it must not depend on anything the operator can change."""

_KNOWN_STATS = set(STAT_KEYS)


def is_known_stat(key: str) -> bool:
    """Does the endpoint read this stat at all?"""
    return key in _KNOWN_STATS


def is_ratio_stat(key: str) -> bool:
    return key == "AF"


def stat_line(key: str, value: Any) -> str:
    """`VPIP=25%` — one stat line in the host's own format. `AF` is the ratio."""
    n = js_round(to_number(value) * 10) / 10
    return f"{key}={js_str(n)}" if is_ratio_stat(key) else f"{key}={js_str(n)}%"


def _readable(v: Any) -> bool:
    """Null and '' are "leave this one to the HUD", not zero."""
    return v is not None and v != "" and is_finite_number(v)


def write_stats(body: Any, by_name: dict | None) -> str:
    """Write stats INTO a body, as if the host's HUD had carried them.

    `by_name` is `{'Big Stack Bob': {'VPIP': 31, 'PFR': 24}}` — the values typed
    by hand (manual.ManualStats). This runs BEFORE the snapshot is parsed, so
    there is one body from there on: what the felt draws, what the exploit gate
    counts and what /move is asked are the same text, and nothing downstream has
    to know a number was typed rather than read.

    Two rules, both of them "look like the host":

      * a key the block already carries is REPLACED in place, so a typed value
        beats the HUD's rather than arriving twice;
      * a key it does not carry is added to that player's FIRST block only,
        above `stack=` — which is where the host puts them, and the only block
        it puts them in (later streets carry the action and the stack alone).

    Everything else is left byte for byte, including the header and the board
    lines. Line endings are normalized to whichever the body already uses, and
    only keys the endpoint actually reads (STAT_KEYS) are written at all.
    """
    text = str(body)
    if not by_name:
        return text

    eol = "\r\n" if "\r\n" in text else "\n"
    out: list[str] = []
    block: list[str] = []
    saw_player = False
    done: set[str] = set()

    def flush() -> None:
        nonlocal block, saw_player
        if not block:
            return
        # The same test the parser makes: a leading block with no `position=` is
        # the tournament header, not a seat named after its first line.
        if not saw_player and not any(_POSITION_LINE.search(line) for line in block):
            out.extend(block)
        else:
            saw_player = True
            out.extend(_write_block_stats(block, by_name, done))
        block = []

    for line in re.split(r"\r?\n", text):
        trimmed = line.strip()
        if not trimmed or _is_board_line(trimmed):
            flush()
            out.append(line)
            continue
        block.append(line)
    flush()
    return eol.join(out)


def _write_block_stats(block: list[str], by_name: dict, done: set[str]) -> list[str]:
    name = block[0].strip()
    stats = by_name.get(name)
    if not stats:
        return block

    wanted = [k for k in STAT_KEYS if _readable(stats.get(k))]
    if not wanted:
        return block

    missing = set(wanted)
    lines = []
    for line in block:
        eq = line.find("=")
        if eq < 0:
            lines.append(line)
            continue
        key = line[:eq].strip().upper()
        if key not in missing:
            lines.append(line)
            continue
        missing.discard(key)
        lines.append(stat_line(key, stats[key]))

    if missing and name not in done:
        added = [stat_line(k, stats[k]) for k in wanted if k in missing]
        at = next((i for i, line in enumerate(lines) if re.match(r"^\s*stack\s*=", line, re.I)), -1)
        if at < 0:
            lines.extend(added)
        else:
            lines[at:at] = added
    done.add(name)
    return lines


# --- the tournament header -------------------------------------------------------

TOURNAMENT_KEYS = ["playersLeft", "playersPaid", "averageStack"]
"""What the header carries, in the order the felt and the details pane list it."""

TOURNAMENT_WRITE_ORDER = ["playersLeft", "averageStack", "playersPaid"]
"""The same three in the order the HOST's own prose puts them
(`781 players left, 78.9BB average stack, 92 players paid`).

The parsers on both ends are regexes and do not care, so this is only about the
body looking like one the client wrote — which is the same reason the typed
stats go in above `stack=` rather than at the end of the block."""

# The wordings the header parser reads, with the NUMBER first — patched in place.
_TOURNAMENT_RE = {
    "playersLeft": re.compile(r"(\d[\d,]*)(\s*players?\s+left)", re.I),
    "playersPaid": re.compile(r"(\d[\d,]*)(\s*players?\s+paid)", re.I),
    "averageStack": re.compile(r"(\d+(?:\.\d+)?)(\s*BB\s+average)", re.I),
}

# The fallback wording, where the number comes second (`average stack 41.2`).
_AVERAGE_STACK_ALT = re.compile(r"(average\s+stack[^\d]*)(\d+(?:\.\d+)?)", re.I)

HEADER_LEAD = "This is online poker tournament"
"""The sentence the host opens a tournament snapshot with."""


def _tournament_number(key: str, value: Any) -> str:
    """Counts are whole; the average stack is a BB figure, to one decimal."""
    n = to_number(value)
    if key == "averageStack":
        return f"{js_round(n * 10) / 10:.1f}"
    return js_str(js_round(n))


def tournament_phrase(key: str, value: Any) -> str:
    """`92 players paid` — one clause of the header, in the host's own words."""
    n = _tournament_number(key, value)
    if key == "averageStack":
        return f"{n}BB average stack"
    return f"{n} players {'left' if key == 'playersLeft' else 'paid'}"


def tournament_header_line(values: dict | None) -> str:
    """The whole header line, for a body that arrived without one."""
    values = values or {}
    parts = [tournament_phrase(k, values[k]) for k in TOURNAMENT_WRITE_ORDER if _readable(values.get(k))]
    return f"{HEADER_LEAD}, {', '.join(parts)}" if parts else ""


def _first_player_line(lines: list[str]) -> int:
    """Where the first player block starts — everything above it is the header."""
    block_start = -1
    for i, line in enumerate(lines):
        trimmed = line.strip()
        if not trimmed or _is_board_line(trimmed):
            block_start = -1
            continue
        if block_start < 0:
            block_start = i
        if _POSITION_LINE.search(line):
            return block_start
    return len(lines)


def _patch_header(text: str, values: dict, wanted: list[str]) -> str:
    """Rewrite the header prose so it says `values`.

    A clause the header already has is replaced where it stands — so a typed
    count beats the client's rather than arriving twice — and one it does not
    have is appended to the sentence, ahead of `Total pot` where there is one:
    that clause closes the host's line, and a phrase after it would read as part
    of the pot to a human and be one regex slip from reading that way to /move.
    """
    out = text
    missing: list[str] = []

    for key in wanted:
        pattern = _TOURNAMENT_RE[key]
        number = _tournament_number(key, values[key])
        if pattern.search(out):
            out = pattern.sub(lambda m, n=number: f"{n}{m.group(2)}", out, count=1)
            continue
        if key == "averageStack" and _AVERAGE_STACK_ALT.search(out):
            out = _AVERAGE_STACK_ALT.sub(lambda m, n=number: f"{m.group(1)}{n}", out, count=1)
            continue
        missing.append(tournament_phrase(key, values[key]))
    if not missing:
        return out

    at = re.search(r"total\s+pot", out, re.I)
    cut = at.start() if at else len(out)
    before = out[:cut]
    # The whitespace ahead of `Total pot` is put back byte for byte: it is a line
    # break whenever the host wrote the pot on its own line, and merging the two
    # would be this function editing something it was not asked about.
    gap = re.search(r"\s*\Z", before).group(0)
    prose = re.sub(r"[.,;]+\Z", "", before[: len(before) - len(gap)], count=1)
    added = ", ".join(missing)
    sentence = f"{prose}, {added}" if prose else added
    if not at:
        return f"{sentence}{gap}"
    return f"{sentence}.{gap or ' '}{out[cut:]}"


def write_tournament(body: Any, values: dict | None) -> str:
    """Write a tournament header INTO a body, as if the client had reported it.

    The counterpart of `write_stats`, and the same bargain: it runs BEFORE the
    parse, so from there on there is one body — the felt, the /move request and
    the failed-call capture all read the same text.

    It matters more than it looks. The endpoint prices a hand under ICM only when
    the header carries players left AND players paid, and only weighs the hero
    against a field when it also carries an average stack; a client that reports
    none of that is answered as a cash game at a pay jump.
    """
    text = str(body)
    if not looks_like_body(text):
        return text

    values = values or {}
    wanted = [k for k in TOURNAMENT_WRITE_ORDER if _readable(values.get(k))]
    if not wanted:
        return text

    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text)
    at = _first_player_line(lines)
    header = lines[:at]

    # No header at all: the whole sentence is ours, blank line and all, which is
    # the shape the host's own snapshot has.
    if not any(line.strip() for line in header):
        return eol.join([tournament_header_line(values), "", *lines[at:]])

    patched = _patch_header("\n".join(header), values, wanted).split("\n")
    return eol.join([*patched, *lines[at:]])
