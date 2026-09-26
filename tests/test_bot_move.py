"""The bot's `move(...)` line: how an answer is worded, and when it is sent."""
from __future__ import annotations

from conftest import FLOP_3WAY, PREFLOP_HU, RIVER_HU, answer, run, settle
from zigsolver_coordinator.bot_move import bot_move
from zigsolver_coordinator.crypto import decrypt_message, encrypt_message
from zigsolver_coordinator.hand_body import parse_hand_body
from zigsolver_coordinator.table import TableSession

_next_index = iter(range(20_000, 30_000))


def move(label: str, body: str = FLOP_3WAY) -> str | None:
    return bot_move({"action": label, "probability": 1.0}, parse_hand_body(body))


def test_the_labels_are_reworded():
    assert move("fold") == "move(fold)"
    assert move("check") == "move(check)"
    assert move("call(4.1BB)") == "move(call)"
    assert move("bet 33%(4.4BB)") == "move(bet 33%(4.4 BB))"
    assert move("raise 60%(27.5BB)") == "move(raise 60%(27.5 BB))"
    assert move("raise 62.5%(12BB)") == "move(raise 62.5%(12.0 BB))"
    assert move("all-in(85.7BB)") == "move(all-in(85.7 BB))"


def test_a_preflop_raise_gets_its_percent_from_the_snapshot():
    # Facing 3BB into 1.5BB with 2BB to call: (8.4 - 3) / (1.5 + 2) = 154%.
    assert move("raise 8.4BB", PREFLOP_HU) == "move(raise 154%(8.4 BB))"


def test_an_all_in_that_only_calls_is_a_call():
    # 4BB to call and 55.6BB behind: a shove is a raise.
    assert move("all-in(55.6BB)", RIVER_HU) == "move(all-in(55.6 BB))"
    short = RIVER_HU.replace("waiting\nhand=A♦J♦\nstack=55.6BB", "waiting\nhand=A♦J♦\nstack=3.2BB")
    assert move("all-in(3.2BB)", short) == "move(call)"
    assert move("all-in", short) == "move(call)"
    # Level with the bet, to the tenth the screen reads.
    level = RIVER_HU.replace("waiting\nhand=A♦J♦\nstack=55.6BB", "waiting\nhand=A♦J♦\nstack=4.0BB")
    assert move("all-in(4.0BB)", level) == "move(call)"


def test_calling_a_shove_the_hero_covers_is_a_call():
    # Bob shoves 51.6BB, the hero has 55.6BB behind: the endpoint labels the
    # call `all-in(51.6BB)`, and it is still only a call.
    shove = RIVER_HU.replace("bet 4BB\nstack=51.6BB", "all in 51.6BB\nstack=0BB")
    assert move("all-in(51.6BB)", shove) == "move(call)"
    # Nobody behind to raise: a call whatever amount the label carries.
    assert move("all-in", shove) == "move(call)"
    assert move("all-in(55.6BB)", shove) == "move(call)"


def test_an_all_in_with_no_amount_is_sized_from_the_stack():
    assert move("all-in", PREFLOP_HU) == "move(all-in(53.8 BB))"


def test_the_frame_is_what_the_host_decrypts():
    frame = encrypt_message("move(bet 33%(4.4 BB))")
    assert frame != "move(bet 33%(4.4 BB))"
    assert decrypt_message(frame) == "move(bet 33%(4.4 BB))"


def session_for(co) -> tuple[TableSession, list[str]]:
    index = next(_next_index)
    session = TableSession(co, index)
    co.tables[index] = session
    sent: list[str] = []
    session.socket.send = lambda text: sent.append(decrypt_message(text)) or True
    return session, sent


def test_nothing_is_sent_while_the_bot_is_off(make, api):
    async def go():
        s, sent = session_for(make())
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert s.result["type"] == "answer" and sent == []
    run(go())


def test_the_drawn_move_is_sent_once_per_decision(make, api):
    async def go():
        co = make()
        api.respond = lambda p: (200, answer(p, [{"action": "bet 33%(4.4BB)", "probability": 1.0}]))
        s, sent = session_for(co)
        co.set_bot(s.index, True)
        assert s.bot_enabled and s.wire("bot") is True
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert sent == ["move(bet 33%(4.4 BB))"]
        # Re-solving the same spot does not make the bot act twice.
        s.solve()
        await settle(0.3)
        assert len(api.moves) == 2 and sent == ["move(bet 33%(4.4 BB))"]
        # The next decision does.
        s.on_host_message(RIVER_HU)
        await settle(0.3)
        assert sent == ["move(bet 33%(4.4 BB))"] * 2
        co.set_bot(s.index, False)
        s.on_host_message(PREFLOP_HU)
        await settle(0.3)
        assert len(sent) == 2
    run(go())


def test_switching_the_bot_on_plays_the_answer_already_up(make, api):
    async def go():
        co = make()
        api.respond = lambda p: (200, answer(p, [{"action": "all-in(3.2BB)", "probability": 1.0}]))
        s, sent = session_for(co)
        short = RIVER_HU.replace("waiting\nhand=A♦J♦\nstack=55.6BB", "waiting\nhand=A♦J♦\nstack=3.2BB")
        s.on_host_message(short)
        await settle(0.3)
        assert sent == []
        co.set_bot(s.index, True)
        assert sent == ["move(call)"]
    run(go())


def test_an_error_sends_nothing(make, api):
    async def go():
        co = make()
        api.respond = lambda p: (400, {"detail": "no"})
        s, sent = session_for(co)
        co.set_bot(s.index, True)
        s.on_host_message(FLOP_3WAY)
        await settle(0.3)
        assert s.result["type"] == "error" and sent == []
    run(go())
