"""The solve pipeline: what goes out for each snapshot, and what comes back on screen.

Each test drives a real TableSession through `on_host_message`, the way the
bot host's socket does, against the fake API in conftest.
"""
from __future__ import annotations

import re

from conftest import (FLOP_3WAY, FLOP_9MAX, PREFLOP_HU, RIVER_HU, TURN_HU, answer, run,
                      settle)
from zigsolver_coordinator.table import TableSession

_next_index = iter(range(100, 10_000))


def session_for(co) -> TableSession:
    """A table session with no socket: the test is the bot host."""
    index = next(_next_index)
    session = TableSession(co, index)
    co.tables[index] = session
    return session


def test_a_snapshot_is_prepared_and_solved(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        # Held for the coalescing window, but already busy.
        assert s.solving and not api.moves
        await settle(0.3)
        assert len(api.moves) == 1
        move = api.moves[0]
        assert move["body"] == FLOP_3WAY
        assert move["regime"] == "gto"
        assert re.fullmatch(rf"t{s.index}#\d+-QsJs", move["handId"])
        assert move["requestId"].endswith("-1")
        assert move["maxSolveTime"] == 15
        # Postflop: which preflop engine is not a question, so it is not sent.
        assert "preflop" not in move
        assert s.result["type"] == "answer" and not s.solving
        assert s.result["clientSeconds"] > 0
        assert [r["street"] for r in s.ranges.streets()] == ["flop"]
        assert co.history.count(s.index) == 1
        assert s.wire("exploit")["why"] == {"key": "reason.multiwayFlop", "count": 3}
    run(go())


def test_a_newer_snapshot_supersedes_and_cancels(make, api):
    async def go():
        co = make()
        api.delay = 1.0
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.25)
        first = api.moves[0]["requestId"]
        api.delay = 0.05
        s.on_host_message(FLOP_9MAX)
        await settle(0.4)
        # POST /cancel names the superseded request by its OWN id.
        assert api.cancels == [first]
        assert len(api.moves) == 2 and api.moves[1]["body"] == FLOP_9MAX
        assert s.result["request"]["body"] == FLOP_9MAX
        assert co.history.count(s.index) == 1
    run(go())


def test_no_cancel_when_the_setting_is_off(make, api):
    async def go():
        co = make()
        co.settings.set("cancelSuperseded", False)
        api.delay = 1.0
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.25)
        s.on_host_message(FLOP_9MAX)
        await settle(0.25)
        assert api.cancels == []
    run(go())


def test_identical_and_partial_rereads_are_ignored(make, api):
    async def go():
        co = make()
        api.delay = 0.5
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.2)
        s.on_host_message(FLOP_3WAY)
        # The same spot with its tail (the hero's own block) cut off.
        s.on_host_message(FLOP_3WAY[: FLOP_3WAY.rindex("*me*")])
        await settle(0.6)
        assert len(api.moves) == 1 and api.cancels == []
        assert s.result["type"] == "answer"
    run(go())


def test_a_drifted_reread_is_shielded(make, api):
    """Same decision, different bytes: sent beside the running solve, and its
    refusal is swallowed (and photographed) rather than shown."""
    drifted = FLOP_3WAY.replace("Big Stack Bob\nposition=BB\ncall", "Big stack Bob\nposition=BB\ncall")

    def respond(payload):
        if "Big stack Bob" in payload["body"]:
            return 400, {"detail": "the pot does not balance"}, 0.01
        return 200, answer(payload), 0.5

    async def go():
        co = make()
        api.respond = respond
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.2)
        s.on_host_message(drifted)
        await settle(0.25)
        # Two calls out, none cancelled, and the refusal is not on screen.
        assert len(api.moves) == 2 and api.cancels == []
        assert s.result is None and s.solving
        await settle(0.4)
        assert s.result["type"] == "answer"
        assert len(api.screen_errors) == 1
        assert api.screen_errors[0]["error"] == "the pot does not balance"
    run(go())


def test_manual_holds_the_solve_until_picked_then_keeps_the_pick(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.regime.select("manual")
        s.on_host_message(TURN_HU)
        await settle(0.3)
        assert not api.moves
        assert s.wire("pending") == {"hand": "AdJd", "street": "turn"}
        s.solve("exploit")
        await settle(0.1)
        assert api.moves[-1]["regime"] == "exploit"
        assert s.pending is None
        # The next street of the same hand is answered under the same pick.
        s.on_host_message(RIVER_HU)
        await settle(0.3)
        assert len(api.moves) == 2 and api.moves[-1]["regime"] == "exploit"
        assert s.pending is None
    run(go())


def test_manual_does_not_ask_where_the_choice_is_moot(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.regime.select("manual")
        s.on_host_message(FLOP_3WAY)  # multiway: GTO whatever is picked
        await settle(0.3)
        assert s.pending is None and api.moves[-1]["regime"] == "gto"
    run(go())


def test_the_advanced_coin_is_drawn_once_per_hand(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.regime.select("advanced")
        s.regime.set_exploit_pct(100)
        s.regime.set_require_stats(False)
        s.on_host_message(TURN_HU)
        await settle(0.3)
        assert api.moves[-1]["regime"] == "exploit"
        assert s.drew == {"coin": "exploit", "regime": "exploit", "forced": None}
        # The mix moves mid-hand; the hand keeps the coin it drew.
        s.regime.set_exploit_pct(0)
        s.on_host_message(RIVER_HU)
        await settle(0.3)
        assert api.moves[-1]["regime"] == "exploit"
    run(go())


def test_the_advanced_gate_forces_gto_on_a_thin_read(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.regime.select("advanced")
        s.regime.set_exploit_pct(100)
        thin = re.sub(r"(?m)^(VPIP|PFR|3BET|ATS|WTSD|AF|Flop Fold to C-BET)=.*\n", "", TURN_HU)
        s.on_host_message(thin)
        await settle(0.3)
        assert api.moves[-1]["regime"] == "gto"
        assert s.drew["forced"] == {"key": "reason.noStats"}
    run(go())


def test_the_preflop_engine_and_its_fallback(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.preflop.select("gto")
        s.on_host_message(PREFLOP_HU)
        await settle(0.3)
        # No /health yet: the blueprint is not known to be there.
        assert api.moves[-1]["preflop"] == "alg"
        assert s.pf_drew["forced"] == {"key": "reason.noPreflopService"}
        co.lobby.gto_available = True
        s.solve()
        await settle(0.1)
        assert api.moves[-1]["preflop"] == "gto" and s.pf_drew is None
        # Preflop is not what the regime is about: always sent as gto.
        assert api.moves[-1]["regime"] == "gto"
    run(go())


def test_each_table_plays_its_own_picks(make, api):
    async def go():
        co = make()
        co.lobby.gto_available = True
        mine, other = session_for(co), session_for(co)
        mine.regime.select("exploit")
        mine.preflop.select("gto")
        for s in (mine, other):
            s.on_host_message(TURN_HU)
        await settle(0.3)
        # Exploit picked on one table is that table's question, not the other's.
        sent = {m["handId"].split("#")[0]: m["regime"] for m in api.moves}
        assert sent == {f"t{mine.index}": "exploit", f"t{other.index}": "gto"}
        assert other.wire("regime") == {"selected": "gto", "exploitPct": 50, "requireStats": True}
        # And the preflop engine the same.
        for s in (mine, other):
            s.on_host_message(PREFLOP_HU)
        await settle(0.3)
        sent = {m["handId"].split("#")[0]: m["preflop"] for m in api.moves[2:]}
        assert sent == {f"t{mine.index}": "gto", f"t{other.index}": "alg"}
    run(go())


def test_a_finished_hand_is_filed_and_never_sent(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.on_host_message(RIVER_HU)
        await settle(0.3)
        s.on_host_message(RIVER_HU + "\nHand finished\n")
        await settle(0.3)
        assert len(api.moves) == 1
        assert s.hand["finished"] and s.last_body is None
        assert s.wire("canSolve") is False
        hands = co.history.hands_for(s.index)
        assert len(hands) == 1 and hands[0]["final"] is not None
    run(go())


def test_a_new_hand_line_clears_the_table(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        s.on_host_message("New hand #4712")
        assert s.hand is None and s.result is None and not s.ranges.streets()
        assert [f["text"] for f in s.feed] == ["New hand #4712"]
        # The history keeps what the table forgot.
        assert co.history.count(s.index) == 1
    run(go())


def test_a_stable_sample_survives_a_resolve(make, api):
    async def go():
        co = make()
        co.settings.set("stableSample", True)
        s = session_for(co)
        api.respond = lambda p: (200, answer(p, [{"action": "check", "probability": 1.0},
                                                 {"action": "bet 33%(4.4BB)", "probability": 0.0}]))
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert s.result["sampled"]["action"] == "check"
        api.respond = lambda p: (200, answer(p, [{"action": "check", "probability": 0.01},
                                                 {"action": "bet 33%(4.4BB)", "probability": 0.99}]))
        for _ in range(5):
            s.solve()
            await settle(0.1)
            assert s.result["sampled"]["action"] == "check"
    run(go())


def test_typed_stats_restate_the_body_and_bump_the_cache_key(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        first_id = api.moves[-1]["handId"]
        co.manual_stats.set(s.index, "Sasha M", "VPIP", 41)
        co._typed(s.index)
        sasha = next(x for x in s.hand["seats"] if x["name"] == "Sasha M")
        assert sasha["stats"] == {"VPIP": 41}
        assert "VPIP=41%" in s.last_body
        # The placeholder side still reads the host's own (empty) value.
        assert s.wire("host")["stats"]["Sasha M"] == {}
        assert s.wire("manual")["stats"]["Sasha M"]["lines"] == ["VPIP=41%"]
        s.resolve_typed(True)
        await settle(0.1)
        assert api.moves[-1]["handId"] == f"{first_id}s1"
        assert "VPIP=41%" in api.moves[-1]["body"]
        # The host re-reading the same spot is still the same spot.
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert len(api.moves) == 2
    run(go())


def test_an_unreachable_api_is_shown_but_not_photographed(make, api):
    async def go():
        co = make()
        api.unreachable = True
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert s.result["type"] == "error"
        assert s.result["message"]["key"] == "api.unreachable"
        await settle(0.1)
        assert api.screen_errors == [] and api.images == []
    run(go())


def test_a_refused_body_is_photographed(make, api):
    async def go():
        co = make()
        api.respond = lambda p: (400, {"detail": "no hero decision"})
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.4)
        assert s.result["type"] == "error" and s.result["status"] == 400
        assert s.result["hint"] == {"key": "api.refusedHint"}
        assert api.images == [f"/image/{s.index}"]
        record = api.screen_errors[0]
        assert record["body"] == FLOP_3WAY and record["status"] == 400
        assert record["image"]  # base64 of the PNG, untouched
        # The same failure again is the same bug: photographed once.
        s.solve()
        await settle(0.2)
        assert len(api.screen_errors) == 1
    run(go())


def test_selecting_manual_mid_hand_asks_and_gto_resolves(make, api):
    async def go():
        co = make()
        s = session_for(co)
        s.on_host_message(TURN_HU)
        await settle(0.3)
        s.regime.select("manual")
        s.on_regime_selected("manual")
        assert s.wire("pending") == {"hand": "AdJd", "street": "turn"}
        s.regime.select("gto")
        s.on_regime_selected("gto")
        await settle(0.1)
        assert s.pending is None and len(api.moves) == 2
    run(go())


def test_autosolve_off_draws_but_does_not_solve(make, api):
    async def go():
        co = make()
        co.settings.set("autoSolve", False)
        s = session_for(co)
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert s.hand is not None and not api.moves and s.wire("canSolve")
        s.solve()
        await settle(0.1)
        assert len(api.moves) == 1
    run(go())
