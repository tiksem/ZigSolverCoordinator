"""One table: one socket to the bot host, one API, any number of front ends.

The mode=0 socket pushes the table snapshots (the /move `body` format) and takes
the commands. Every snapshot is prepared here — the typed stats and header
written into it, parsed, placed in its hand — and POSTed to the ZigSolver API,
and the answer is what every front end watching the table renders. A second
page on the same table joins this session instead of opening a second socket
and paying for a second solve of every snapshot.

The regime picks which question goes out with the snapshot — THIS table's
regime, like its preflop engine: each table keeps its own (per_table.py), so
picking on one table's bar leaves the others' play alone. `manual` is the one
that changes the flow rather than the payload: the solve is HELD until the
operator picks, so a decision they have not answered yet has spent no budget. It
is only held where the choice matters — a spot the exploit regime cannot answer
would come back GTO either way, so it goes straight out.

Both of the modes that CHOOSE a regime — manual's answer and advanced's coin —
are decided once per hand and reused by that hand's later streets, so the turn
is answered by whatever the flop was. `HandIds` draws the boundary: it changes
when the hero's cards change or the board or the pot goes backwards, which
means it does not depend on the host announcing a new hand.

A port of the front end's TableView, which ran all of this in the browser.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from .answers import next_id, now_ms
from .bot_host import HostSocket
from .bot_move import bot_move
from .crypto import encrypt_message
from .endpoints import MODE_HAND
from .hand_body import decision_key, is_hand_over_line, parse_hand_body
from .preflop import PreflopState
from .ranges import HandRanges
from .regime import RegimeState, exploit_availability, resolve_advanced, villain_read
from .settings import REQUEST_FIELDS

if TYPE_CHECKING:
    from .server import Coordinator

log = logging.getLogger("zsc.table")

COALESCE = 0.15
"""A new snapshot lands while an older one is still solving all the time (the
villain acts, the client re-reads). The short window before firing coalesces a
burst of snapshots into one solve — the case where cancelling after the fact
would already have spent the budget. It is invisible next to a multi-second
solve."""

FEED_MAX = 200

FIELDS = ["socket", "hand", "result", "solving", "solveElapsed", "pending", "regime", "preflop",
          "exploit", "villainStats", "drew", "pfDrew", "canSolve", "ranges", "feed", "history",
          "manual", "host", "bot"]
"""Everything a front end is told about a table, and the names it is told them by."""

_counters: dict[int, itertools.count] = {}


class HandIds:
    """Tracks which poker hand a snapshot belongs to and mints its handId.

    A hand is "the same one" while the hero holds the same two cards, the board
    only grows, and the pot only grows. Any of those going backwards means a new
    hand — which is what makes this independent of whether the host bothered to
    announce one.

    The count is per table and per PROCESS, not per session: a table whose last
    front end left and came back is a new session, and handing it `t3#1` again
    would file its first hand under an old one.
    """

    def __init__(self, table_index: int) -> None:
        self.index = table_index
        self._counter = _counters.setdefault(table_index, itertools.count(1))
        self._seq = 0
        self._prev: tuple[str, str, float] | None = None

    def __call__(self, snapshot: dict) -> str:
        hero = "".join(snapshot["heroHand"]) if snapshot["heroHand"] else ""
        board = "".join(snapshot["board"])
        pot = snapshot["pot"] if snapshot["pot"] is not None else 0
        prev = self._prev
        same = prev is not None and prev[0] == hero and board.startswith(prev[1]) and pot >= prev[2] - 1e-9
        if not same:
            self._seq = next(self._counter)
        self._prev = (hero, board, pot)
        return f"t{self.index}#{self._seq}{f'-{hero}' if hero else ''}"


class TableSession:
    def __init__(self, co: Coordinator, index: int) -> None:
        self.co = co
        self.index = index
        self.subscribers: set = set()
        self.linger: asyncio.TimerHandle | None = None
        self.closed = False

        # --- what the front ends draw ---------------------------------------
        self.hand: dict | None = None
        self.result: dict | None = None
        self.solving = False
        # time.monotonic() at the moment the /move request went out, or None.
        # Front ends count up from it while the answer is outstanding, and the
        # same clock is stopped on arrival and carried on the result as
        # `clientSeconds` — so what the operator watched tick and what is
        # compared against the API's own `responseTime` are one measurement.
        self.solve_started_at: float | None = None
        self.feed: list[dict] = []
        # Manual mode: the snapshot waiting for a regime to be picked. Holding
        # the SNAPSHOT rather than a flag, because a newer one supersedes an
        # unanswered question — nobody should be answering a spot the table has
        # already moved past.
        self.pending: dict | None = None
        # How this hand's Advanced coins came up, for the regime bar to report.
        self.drew: dict | None = None
        self.pf_drew: dict | None = None
        # The ranges this hand has been solved on, one record per street.
        self.ranges = HandRanges()

        # --- what drives the solves -------------------------------------------
        # The snapshot currently being answered — what a re-solve re-sends.
        self.last_body: str | None = None
        # The same snapshot as the HOST wrote it, before anything typed was
        # written into it. Kept beside `last_body` because typing rebuilds the
        # body of the spot already on screen, and the rewrite is not reversible:
        # writing over an already-written body would replace a value happily and
        # could never take a line back out, so CLEARING a typed one would leave
        # it in the snapshot.
        self.host_body: str | None = None
        self.last_hand_id: str | None = None
        self.solve_seq = 0
        # The seq of the answer on screen — nothing older may overwrite it.
        # "Are you the newest request?" would throw away an older solve's answer
        # the moment a shielded re-read went out behind it, which is precisely
        # the answer worth keeping.
        self.accepted_seq = 0
        # The solves in flight, oldest first: seq -> {task, rid, key}. Usually
        # one; more only while a SHIELDED solve runs — a re-read of the decision
        # already being answered, sent without tearing down the answer in
        # progress. Every entry is therefore the same question, which is what
        # makes "the first one to answer wins, and cancels the rest" right.
        self.live: dict[int, dict] = {}
        self._coalesce: asyncio.TimerHandle | None = None
        # Cancellation handles: every solve gets its OWN id, and /cancel names it.
        # A stable per-table id looks tidier and is wrong: /cancel is
        # fire-and-forget, so it races the replacement /move, and if the
        # replacement wins the race the cancel arrives afterwards and kills the
        # request just made. With a unique id a late cancel names a request that
        # has already finished, which the endpoint answers with `found: false`.
        self._id_base = f"zs-{secrets.token_hex(3)}-t{index}"
        # How many times what is typed by hand has changed here — it rides on the
        # handId. A tree built while the villain had no read, or while the body
        # said nothing about the payout ladder, is not the tree this question
        # wants: typing either is a different question about the same hand, so
        # it asks under a different id rather than collecting a cache hit.
        self.body_rev = 0
        self.hand_ids = HandIds(index)

        # The decision the last `move(...)` went out for, so a re-solve of the
        # same spot does not tell the bot to act twice.
        self._moved_key: str | None = None

        # This hand's choice under the two modes that make one, keyed by the hand
        # it was made for. A key that no longer matches `last_hand_id` is a stale
        # answer to a hand that is over — the same as not having one.
        self._coin: tuple[str | None, str | None] = (None, None)
        self._pick: tuple[str | None, str | None] = (None, None)
        self._pf_coin: tuple[str | None, str | None] = (None, None)

        self.socket = HostSocket(lambda: co.endpoints.socket_url(MODE_HAND, index),
                                 self.on_host_message, lambda _status: self.changed("socket"),
                                 name=f"table {index}")

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        self.socket.open()

    def close(self) -> None:
        """The last front end has gone: nothing solves for a table nobody reads."""
        self.closed = True
        self.abort_in_flight()
        self._clear_coalesce()
        self.socket.close()

    # --- this table's picks -----------------------------------------------------------

    @property
    def regime(self) -> RegimeState:
        """The regime picked on this table's bar — kept by the coordinator, so it
        outlives the session."""
        return self.co.regime.of(self.index)

    @property
    def preflop(self) -> PreflopState:
        return self.co.preflop.of(self.index)

    # --- what the front ends are told -------------------------------------------

    def changed(self, *fields: str) -> None:
        if "hand" in fields:
            fields = (*fields, "exploit", "villainStats", "canSolve")
        self.co.table_changed(self, fields)

    def wire(self, field: str) -> Any:
        if field == "socket":
            return self.socket.status
        if field == "hand":
            return self.hand
        if field == "result":
            return self.result
        if field == "solving":
            return self.solving
        if field == "solveElapsed":
            # Elapsed rather than a timestamp: the front end may run on another
            # machine whose clock does not agree with this one.
            return None if self.solve_started_at is None else time.monotonic() - self.solve_started_at
        if field == "pending":
            p = self.pending
            return {"hand": "".join(p["heroHand"]) if p["heroHand"] else None,
                    "street": p["streetName"]} if p else None
        if field == "regime":
            return self.regime.to_wire()
        if field == "preflop":
            return self.preflop.to_wire()
        if field == "exploit":
            return exploit_availability(self.hand)
        if field == "villainStats":
            return villain_read(self.hand)["stats"]
        if field == "drew":
            return self.drew
        if field == "pfDrew":
            return self.pf_drew
        if field == "canSolve":
            return bool(self.last_body) and self.co.endpoints.api is not None
        if field == "ranges":
            return self.ranges.streets()
        if field == "feed":
            return self.feed
        if field == "history":
            return {"count": self.co.history.count(self.index), "rev": self.co.history.rev(self.index)}
        if field == "manual":
            return {"stats": self.co.manual_stats.to_wire(self.index),
                    "tournament": self.co.manual_tournament.to_wire(self.index)}
        if field == "host":
            return self._host_values()
        if field == "bot":
            return self.bot_enabled
        raise KeyError(field)

    def patch(self, fields) -> dict:
        return {f: self.wire(f) for f in FIELDS if f in fields}

    def full(self) -> dict:
        return self.patch(FIELDS)

    def _host_values(self) -> dict:
        """What the HOST's own snapshot said, before anything typed was written
        into it — the editors' placeholders, so an empty field reads as the value
        it is leaving alone rather than as nothing."""
        host = parse_hand_body(self.host_body) if self.host_body else None
        if not host:
            return {"stats": {}, "tournament": None}
        return {"stats": {s["name"]: s["stats"] for s in host["seats"] if not s["isHero"]},
                "tournament": host["tournament"]}

    def _notify(self, **message: Any) -> None:
        self.co.notify(scope="table", index=self.index, **message)

    # --- small state setters that know what they change -----------------------------

    def _set_pending(self, value: dict | None) -> None:
        if value is not self.pending:
            self.pending = value
            self.changed("pending")

    def _set_started(self, value: float | None) -> None:
        if value != self.solve_started_at:
            self.solve_started_at = value
            self.changed("solveElapsed")

    def _set_drew(self, value: dict | None) -> None:
        if value != self.drew:
            self.drew = value
            self.changed("drew")

    def _set_pf_drew(self, value: dict | None) -> None:
        if value != self.pf_drew:
            self.pf_drew = value
            self.changed("pfDrew")

    def _update_busy(self) -> None:
        busy = bool(self.live) or self._coalesce is not None
        if busy != self.solving:
            self.solving = busy
            self.changed("solving")

    def _clear_coalesce(self) -> None:
        if self._coalesce is not None:
            self._coalesce.cancel()
            self._coalesce = None

    # --- the per-hand choices ---------------------------------------------------------

    def _hand_coin(self) -> str:
        """Advanced: this hand's coin, flipped on its FLOP and kept for the rest.

        Only ever called once the hand is past preflop, so the flip lands on the
        street the mode says it does."""
        if self._coin[0] != self.last_hand_id:
            self._coin = (self.last_hand_id, self.regime.flip_coin())
        return self._coin[1]

    def _hand_pick(self) -> str | None:
        """Manual: what the operator answered for this hand, or None."""
        return self._pick[1] if self._pick[0] == self.last_hand_id else None

    def _hand_preflop_coin(self) -> str:
        """Advanced preflop: drawn on the hand's FIRST PREFLOP DECISION and kept
        for the rest of its preflop — a hand is one line."""
        if self._pf_coin[0] != self.last_hand_id:
            self._pf_coin = (self.last_hand_id, self.preflop.flip_coin())
        return self._pf_coin[1]

    def forget_hand_choice(self) -> None:
        self._coin = (None, None)
        self._pick = (None, None)
        self._pf_coin = (None, None)

    def regime_to_send(self, picked: str | None = None) -> str:
        """The regime to actually send.

        `manual` and `advanced` are not ones — they are how the regime for THIS
        HAND is chosen, by asking or by drawing on the flop, and either way the
        answer is reused by the turn and the river rather than re-asked.
        `picked` is the manual answer on its way in, and it is recorded against
        the hand.

        Preflop leaves all of that alone. It goes out as `gto` because that is
        the endpoint's name for "play it with the preflop engine", and it
        neither spends the hand's coin nor counts as the hand being answered.
        """
        regime = self.regime
        if picked and regime.selected == "manual":
            self._pick = (self.last_hand_id, picked)

        exploit = exploit_availability(self.hand)
        preflop = bool(self.hand) and not self.hand["street"]
        if exploit["status"] == "pending" or (preflop and regime.selected != "exploit"):
            # Preflop is the preflop engine's unless Exploit itself is selected
            # and the all-in calculator applies: manual never asks about it and
            # advanced never draws for it -- the coin waits for the flop.
            self._set_drew(None)
            return "gto"

        if regime.selected == "advanced":
            out = resolve_advanced(self.hand, self._hand_coin(), regime.require_stats)
            self._set_drew(out)
            return out["regime"]
        self._set_drew(None)

        # Manual with nothing answered yet only reaches here on a hand not worth
        # asking about, which is GTO by definition.
        want = (self._hand_pick() or "gto") if regime.selected == "manual" else regime.selected
        if want == "exploit" and not exploit["ok"]:
            return "gto"
        return want

    def preflop_to_send(self) -> str | None:
        """The preflop engine to actually send, and what it cost to get there.

        Only decided for a PREFLOP decision: past the flop the field is irrelevant
        and drawing there would spend the hand's coin on a street it does not
        govern."""
        if self.hand and self.hand["street"]:
            self._set_pf_drew(None)
            return None
        pf = self.preflop
        coin = self._hand_preflop_coin() if pf.selected == "advanced" else None
        out = pf.resolve(coin, self.co.lobby.gto_available)
        self._set_pf_drew(out if pf.selected == "advanced" or out["forced"] else None)
        return out["engine"]

    # --- the snapshots --------------------------------------------------------------

    def with_manual(self, text: str) -> str:
        """The host's snapshot with everything typed by hand written into it — the
        seats' stats and the table's tournament header, in the host's formats.

        One function because there is one body: from here on the felt, the
        exploit gate, the /move request and the screen capture all read the same
        text, and nothing downstream has to know a number was typed."""
        return self.co.manual_tournament.apply(self.index, self.co.manual_stats.apply(self.index, text))

    def _is_partial_reread(self, parsed: dict) -> bool:
        """Is this snapshot LESS of the spot already on the felt, rather than a new one?

        The host builds the body by appending and re-reads the table on a timer,
        so a poll that lands on a spot nothing has happened on can still come back
        DIFFERENT: the tail is missing, most often the hero's own trailing block —
        the one that says whose turn it is. (KScanner appends `*me* waiting` only
        on the poll where the hero BECOMES to act, so every later poll of the same
        decision drops it.)

        Acting on it aborts the solve in flight and starts it again from zero
        for a decision already being answered — two 10s solves for one spot, and
        the first answer thrown away (measured on QJo 3-way, 2026-08-23).

        Two shapes, both meaning "no action has been added since":

          * every byte of the new body is already in the old one — a straight
            truncation, whatever it cut;
          * the same board, and a client that WAS marking someone to act has
            stopped — the partial read that also lost a digit somewhere. Only the
            hero's own block carries that mark on the street being played, so
            losing it is the tell: the host pushes a snapshot to ask what to do,
            and this one asks nothing.
        """
        prev = self.hand
        if not prev or not self.last_body:
            return False
        before = self.last_body.rstrip()
        after = parsed["raw"].rstrip()
        if len(after) < len(before) and before.startswith(after):
            return True
        return bool(prev["waitingOn"]) and not parsed["waitingOn"] and parsed["board"] == prev["board"]

    def _same_decision_as_live(self, parsed: dict) -> bool:
        """Is this snapshot the DECISION already being solved, arriving again?

        `_is_partial_reread` catches the re-read that lost bytes. This catches the
        one that gained wrong ones: same street, same hole cards, same number of
        actions, but a body that has drifted — a name read with a different
        capital, a stack short a digit, an action label the client got wrong.

        That is how the KQo flop on 2026-09-07 lost a 36-second answer: the
        re-read turned the hero's own `raise 2.5BB` into `call`, the endpoint
        refused the body it could no longer balance against the pot, and the good
        solve had already been cancelled to make room for it.

        The caller does not drop the snapshot — a drifted read can also be a
        BETTER one (a HUD block that has finally loaded). It sends it SHIELDED:
        alongside the running solve rather than over its corpse, so a refusal
        costs nothing and an answer is still an answer.
        """
        if not self.live:
            return False
        key = decision_key(parsed)
        return bool(key) and any(s["key"] == key for s in self.live.values())

    def on_host_message(self, text: str) -> None:
        # Whatever has been typed by hand goes in BEFORE the parse, so there is
        # one body from here on.
        parsed = parse_hand_body(self.with_manual(text))
        if parsed:
            # A snapshot that ends on "Hand finished" is a RESULT, not a question.
            # Held here, before anything is sent: the endpoint refuses it, so
            # solving it costs a round trip to be told what we already know.
            if parsed["finished"]:
                if self.hand and self.hand["finished"] and self.hand["raw"] == parsed["raw"]:
                    return
                self.hand_over(parsed)
                return
            # Byte-for-byte the body already on the felt: the client is re-reading
            # a spot nothing has happened on. Ignoring rather than re-solving is
            # what makes a chatty client survivable — a re-read while the solve
            # is in flight would abort it and start again from zero, so a host
            # that re-reads faster than the solver answers would never get one.
            unchanged = parsed["raw"] == self.last_body
            if unchanged and (self.solving or self.result or self.pending):
                return
            # The same spot arriving with its tail cut off: dropped even with
            # nothing on screen yet, since there is no newer question in it.
            if self._is_partial_reread(parsed):
                return
            # The same DECISION under a body that has drifted. Read before the
            # felt moves, because it is a question about what is still running.
            shielded = self._same_decision_as_live(parsed)
            # One shield per decision, and no more. A host that re-reads every
            # second through a 35-second solve would otherwise put a request out
            # for each read, queued behind an answer already given.
            if shielded and len(self.live) > 1:
                return
            self.hand = parsed
            self.last_body = parsed["raw"]
            self.host_body = text
            self.last_hand_id = self.hand_ids(parsed)
            self.changed("hand", "host")
            if self.co.settings["autoSolve"]:
                # Manual mode asks first — and asks about the NEWEST snapshot, so
                # a question for a spot the table has moved past is replaced
                # rather than answered. Once per hand: a later street of a hand
                # already answered goes straight out under that answer.
                if (self.regime.selected == "manual" and self.hand["street"]
                        and exploit_availability(self.hand)["ok"] and not self._hand_pick()):
                    self.abort_in_flight()
                    self._clear_coalesce()
                    self._update_busy()
                    self._set_pending(parsed)
                else:
                    self._set_pending(None)
                    self.schedule_solve(supersede=not shielded)
            return

        # The same news as a plain line, with no snapshot under it: the felt keeps
        # the last decision, but that decision is no longer live.
        if is_hand_over_line(text):
            self.hand_over(None)
        if re.search(r"new hand", text, re.I):
            # The spot on screen is over; anything still solving for it is wasted.
            self.stop_solving()
            self.hand = None
            self.result = None
            self.last_body = None
            self.host_body = None
            self.last_hand_id = None
            self._set_drew(None)
            self.forget_hand_choice()
            # A new hand is being dealt, so the last one's ranges describe a table
            # that is gone. NOT cleared when the hand merely ends: the felt keeps
            # the final table, and the ranges it was played on are still worth
            # reading.
            self.ranges.clear()
            self.feed = []
            self.changed("hand", "result", "host", "ranges")
        # Everything the host says that is not a snapshot: a notification now,
        # and the message dock's history afterwards.
        self._notify(text=text, tone="warn" if re.search(r"not running|error|fail", text, re.I) else "info")
        self.feed = [{"text": text, "at": now_ms()}, *self.feed][:FEED_MAX]
        self.changed("feed")

    def hand_over(self, parsed: dict | None) -> None:
        """The hand is over.

        The final table stays on the felt — with `parsed`, the snapshot that
        carried the marker — but nothing more goes out for it. `last_body` is
        cleared rather than kept, so neither auto-solve nor Re-solve can spend a
        request on a body /move will refuse.
        """
        self.stop_solving()
        if parsed:
            # How the hand ended goes into its history before the hand id is let
            # go of: the decisions stop at the hero's last action, and this is
            # the rest of it.
            if self.co.history.record_hand_end(self.index, self.last_hand_id, parsed["raw"]):
                self.changed("history")
            self.hand = parsed
        self.last_body = None
        self.host_body = None
        self.last_hand_id = None
        self._set_drew(None)
        self.forget_hand_choice()
        self.changed("hand" if parsed else "canSolve", "host")

    def restate(self) -> None:
        """Something typed changed: restate the snapshot on screen under it, so
        the felt and the answer's body are the text that will actually go out.

        The hand id is deliberately not re-minted — this is the same hand and the
        same decision. Which read it is asked under is `body_rev`'s job."""
        if not self.host_body:
            return
        parsed = parse_hand_body(self.with_manual(self.host_body))
        if not parsed:
            return
        self.hand = parsed
        self.last_body = parsed["raw"]
        self.changed("hand")

    # --- the solves ------------------------------------------------------------------

    def stop_solving(self) -> None:
        """Drop whatever is solving or waiting to be asked: its spot is gone."""
        self.abort_in_flight()
        self._clear_coalesce()
        self._set_started(None)
        self._set_pending(None)
        self._update_busy()

    def _drop(self, seq: int) -> None:
        """Kill one solve — on both ends — and forget it."""
        s = self.live.pop(seq, None)
        if not s:
            return
        s["task"].cancel()
        # Abandoning the request only frees this end; POST /cancel is what stops
        # the solver and releases its semaphore, so the replacement is not queued
        # behind an answer nobody will read.
        if self.co.settings["cancelSuperseded"]:
            self.co.api.cancel(s["rid"])

    def abort_in_flight(self) -> None:
        """Kill every request in flight: the spot is gone — a new hand, the hand
        ending, a regime picked by hand. Shielded or not, they all go."""
        for seq in list(self.live):
            self._drop(seq)

    def _abort_others(self, keep: int) -> None:
        """Kill every solve except `keep` — the decision it asked about is answered."""
        for seq in list(self.live):
            if seq != keep:
                self._drop(seq)

    def schedule_solve(self, *, supersede: bool = True) -> None:
        """Solve the newest snapshot, after the coalescing window.

        `supersede=False` leaves whatever is running alone — the caller decided
        this snapshot asks the SAME question as a solve already in flight, so the
        two race instead of one replacing the other. Nothing doubles up on the
        box for long: the endpoint serialises solves on one semaphore and refuses
        a body it cannot balance before taking it, so the shielded call either
        fails in milliseconds or waits, and the first real answer cancels
        whatever is left.
        """
        if supersede:
            self.abort_in_flight()
            # The superseded solve's clock stops with it: the coalescing window
            # belongs to the replacement.
            self._set_started(None)
        self._clear_coalesce()

        def fire() -> None:
            self._coalesce = None
            self.solve(supersede=supersede)

        self._coalesce = asyncio.get_running_loop().call_later(COALESCE, fire)
        self._update_busy()

    def cache_key(self) -> str | None:
        """The id this hand's tree is cached under, at the read it is asked on."""
        if not self.last_hand_id:
            return None
        return f"{self.last_hand_id}s{self.body_rev}" if self.body_rev else self.last_hand_id

    def solve(self, picked: str | None = None, *, supersede: bool = True) -> None:
        """POST the current snapshot under `picked`, or the selected regime.

        `supersede=False` is the shielded call — see `schedule_solve`."""
        self._clear_coalesce()
        self._set_pending(None)
        if self.closed or not self.co.endpoints.api_url("/move") or not self.last_body:
            self._update_busy()
            return

        # A newer snapshot supersedes an older solve: without this a slow answer
        # for a spot that has moved on could land after the fresh one.
        if supersede:
            self.abort_in_flight()
        self.solve_seq += 1
        seq = self.solve_seq
        rid = f"{self._id_base}-{seq}"
        settings = self.co.settings
        request = {
            "body": self.last_body,
            "handId": self.cache_key() if settings["useHandCache"] else None,
            "requestId": rid,
            **{k: settings[k] for k in REQUEST_FIELDS},
            "regime": self.regime_to_send(picked),
            "preflop": self.preflop_to_send(),
        }
        # The hand this question is about, read now rather than when the answer
        # lands: a snapshot arriving mid-solve moves `last_hand_id` on, and filing
        # the answer under it would put this hand's flop in the next hand.
        for_hand = self.last_hand_id
        started = time.monotonic()
        # A shielded call does not restart the clock: the solve it went out
        # beside is still the one being waited on.
        if supersede or self.solve_started_at is None:
            self._set_started(started)

        task = asyncio.create_task(self._run_solve(seq, request, for_hand, started),
                                   name=f"solve t{self.index}#{seq}")
        self.live[seq] = {"task": task, "rid": rid, "key": decision_key(self.hand)}
        task.add_done_callback(lambda _t, s=seq: self._solve_done(s))
        log.info("table %s: solve %s -> regime=%s handId=%s", self.index, rid, request["regime"],
                 request["handId"])
        self._update_busy()

    async def _run_solve(self, seq: int, request: dict, for_hand: str | None, started: float) -> None:
        try:
            out = await self.co.api.move(request)
        except asyncio.CancelledError:
            raise  # superseded — never an error to show
        except Exception as exc:  # noqa: BLE001
            log.exception("table %s: solve %s failed", self.index, request["requestId"])
            self.live.pop(seq, None)
            # Not an error to show while a live sibling may still answer.
            if self.live or seq <= self.accepted_seq:
                return
            self.accepted_seq = seq
            failed = {"type": "error", "id": next_id(), "receivedAt": now_ms(), "message": str(exc),
                      "hint": None, "payload": None, "status": None, "request": request,
                      "clientSeconds": time.monotonic() - started}
            self._show(failed, for_hand)
            self._capture_failure(failed, request)
            return

        client_seconds = time.monotonic() - started
        self.live.pop(seq, None)
        log.info("table %s: solve %s <- %s in %.2fs", self.index, request["requestId"], out["type"],
                 client_seconds)
        if out["type"] == "cancelled":
            return
        # A REFUSAL while a sibling is still solving is the case the shield
        # exists for: the two asked the same question, one of them could not be
        # read, and the one that could is still working. The picture is still
        # filed, because a body the endpoint cannot balance is a MISREAD and the
        # screen is the only place the reason for it exists.
        if out["type"] == "error" and self.live:
            self._capture_failure(out, request)
            return
        if seq <= self.accepted_seq:
            return
        self.accepted_seq = seq
        # The question has been answered; anything still running asked the same
        # one and is now just occupying the solver.
        self._abort_others(seq)
        # What this end actually waited, next to what the API says it spent.
        out["clientSeconds"] = client_seconds
        # Re-solving the same spot should not also re-roll the dice. Only a GTO
        # answer has a draw to hold; an exploit answer's top row is the move.
        #
        # THE SAME SPOT, by the snapshot the two answers were asked on: the answer
        # on screen is not cleared when the table moves, so without this the turn
        # would inherit the flop's draw whenever the key survives the street (a
        # bare `check` almost always does). Identical bodies are exactly the
        # re-solves this setting is about — Re-solve, a regime pick, a settings
        # change — and nothing else.
        prev = self.result
        if (self.co.settings["stableSample"] and out["type"] == "answer" and out["regime"] == "gto"
                and prev and prev.get("type") == "answer" and prev.get("regime") == "gto"
                and (prev.get("request") or {}).get("body") == request["body"]):
            held = prev.get("sampled")
            still = next((a for a in out["actions"] if held and a["action"] == held["action"]), None)
            # A draw the new answer no longer plays is not a draw to keep.
            if still and still["probability"] > 0:
                out["sampled"] = still
        self._show(out, for_hand)
        if out["type"] == "error":
            self._capture_failure(out, request)
        else:
            self._send_bot_move(out)

    def _show(self, out: dict, for_hand: str | None) -> None:
        """Put an answer on screen, and keep it."""
        self.result = out
        self.changed("result")
        # The ranges this answer ran on, under the hand it was asked about — a
        # no-op for the regimes that enumerate none.
        if self.ranges.record(for_hand, out):
            self.changed("ranges")
        # And the answer itself, so the hand can be read again once it is gone.
        if self.co.history.record_solve(self.index, for_hand, out):
            self.changed("history")

    @property
    def bot_enabled(self) -> bool:
        """Whether the bot plays this table — the coordinator's switch (bots.py),
        which outlives this session."""
        return self.co.bots.is_on(self.index)

    def bot_switched(self, on: bool) -> None:
        """The switch moved: said on the table the way any of its news is."""
        text = "Bot enabled" if on else "Bot disabled"
        self._notify(text=text, tone="info")
        self.feed = [{"text": text, "at": now_ms()}, *self.feed][:FEED_MAX]
        self.changed("feed", "bot")
        # Switched on while the answer to the spot on screen is already up:
        # that answer is the move.
        if on and not self.solving and self.result and self.last_body:
            self._send_bot_move(self.result)

    def _send_bot_move(self, out: dict) -> None:
        """Tell the host's bot what to play: the answer's drawn move, encrypted,
        on this table's socket — when the bot plays this table, the hero is the
        one to act, and this decision has not already been sent. The host plays
        whatever it is sent, so this is the only gate there is."""
        if not self.bot_enabled or out.get("type") != "answer":
            return
        body = (out.get("request") or {}).get("body")
        # Only the answer to the spot on screen: anything older is a spot gone.
        if not body or body != self.last_body:
            return
        hand = parse_hand_body(body)
        if not hand or not hand["heroToAct"]:
            return
        # Under the hand id too: the same two cards can be dealt twice running.
        key = f"{self.last_hand_id}|{decision_key(hand)}"
        if key == self._moved_key:
            return
        move = bot_move(out.get("sampled"), hand)
        if move is None:
            log.warning("table %s: no bot move for %r", self.index, (out.get("sampled") or {}).get("action"))
            return
        if not self.socket.send(encrypt_message(move)):
            log.warning("table %s: %s not sent, the host socket is not open", self.index, move)
            return
        self._moved_key = key
        log.info("table %s: sent %s", self.index, move)

    def _solve_done(self, seq: int) -> None:
        self.live.pop(seq, None)
        # The clock belongs to the solves still out: an older solve finishing (or
        # being superseded) must not stop the timer a replacement just started.
        if not self.live:
            self._set_started(None)
        self._update_busy()

    def _capture_failure(self, err: dict, request: dict) -> None:
        """A call came back an error: photograph the table it failed on.

        Fire-and-forget, and quiet unless it works: the operator has already been
        shown the error that matters."""
        message = err.get("message")
        # Nothing to send it to: the endpoint being unreachable is the one error
        # whose capture cannot be delivered, and the screenshot is megabytes.
        if isinstance(message, dict) and message.get("key") == "api.unreachable":
            return
        failure = {
            "tableIndex": self.index,
            "handId": request.get("handId") or self.last_hand_id,
            "requestId": request.get("requestId"),
            "regime": request.get("regime"),
            "street": self.hand["streetName"] if self.hand else None,
            "error": message,
            "hint": err.get("hint"),
            "status": err.get("status"),
            "body": request.get("body"),
        }

        async def capture() -> None:
            out = await self.co.screen_errors.capture(failure)
            if out["ok"]:
                self._notify(key="table.screenCaptured", tone="info")
            elif out.get("reason"):
                log.warning("table %s: screen not captured: %s", self.index, out["reason"])

        self.co.spawn(capture())

    # --- what the operator does -----------------------------------------------------------

    def on_regime_selected(self, value: str) -> None:
        """A regime was picked on this table's bar.

        Picking a regime is a deliberate gesture about the hand in front of you,
        so it drops whatever that hand had already been assigned — otherwise
        clicking Manual mid-hand would silently reuse the answer you clicked in
        order to change, and clicking Advanced would keep a coin drawn at the old
        mix."""
        self.forget_hand_choice()
        if value == "manual":
            # Switching INTO manual mid-hand asks about the spot on screen rather
            # than waiting for the table to move; the answer up already stays
            # until the question is answered.
            self.abort_in_flight()
            self._update_busy()
            if (self.last_body and self.hand and self.hand["street"]
                    and exploit_availability(self.hand)["ok"]):
                self._set_pending(self.hand)
            return
        self._set_pending(None)
        # Advanced opens its knobs on the same click, and the front end re-solves
        # when they close, so the draw is made at the mix just settled on.
        if value == "advanced":
            return
        if self.last_body:
            self.solve()

    def resolve_typed(self, changed: bool) -> None:
        """An editor closed: re-ask the spot if what it writes into the body changed.

        Closing the editor is what re-solves, not every keystroke: the question
        goes out once, under the numbers the operator settled on."""
        if not changed:
            return
        # The body has changed, so the tree cached against this hand was solved
        # on ranges — or a pay ladder — it no longer describes.
        self.body_rev += 1
        # A manual question still waiting stays waiting: re-solving here would
        # answer it as GTO on the operator's behalf.
        if self.pending or not self.last_body:
            return
        self.solve()

    def clear_feed(self) -> None:
        self.feed = []
        self.changed("feed")
