"""What is kept: endpoints, settings, the modes, typed values, history, files."""
from __future__ import annotations

import json
import os
import stat

from conftest import FLOP_3WAY, RIVER_HU, answer
from zigsolver_coordinator.answers import build_answer, build_error
from zigsolver_coordinator.endpoints import Endpoints, parse_server
from zigsolver_coordinator.history import PER_TABLE, History
from zigsolver_coordinator.manual import MAX_NAMES, ManualStats, ManualTournament
from zigsolver_coordinator.per_table import PerTable
from zigsolver_coordinator.preflop import PreflopState, gto_available
from zigsolver_coordinator.regime import RegimeState, exploit_availability
from zigsolver_coordinator.settings import DEFAULTS, Settings
from zigsolver_coordinator.store import Store
from zigsolver_coordinator.hand_body import parse_hand_body


def test_parse_server_takes_what_people_type():
    t = parse_server("localhost:8080")
    assert (t.secure, t.host, t.prefix) == (False, "localhost:8080", "")
    t = parse_server("wss://box:8443/app/")
    assert (t.secure, t.host, t.prefix) == (True, "box:8443", "/app")
    assert parse_server("http://10.0.0.4:8000").host == "10.0.0.4:8000"
    for bad in ["", "   ", None, "http://", "host:99999", "a b:80"]:
        assert parse_server(bad) is None, bad


def test_endpoints_build_urls_and_keep_the_token_to_the_api():
    e = Endpoints({"host": "box:8080/app", "api": "https://api:8000", "apiToken": "s3cret", "connected": True})
    assert e.socket_url(0, 3) == "ws://box:8080/app/commands?mode=0&tableIndex=3"
    assert e.http_url("/image/3") == "http://box:8080/app/image/3"
    assert e.api_url("/move") == "https://api:8000/move"
    assert e.api_headers() == {"Authorization": "Bearer s3cret"}
    wire = e.to_wire()
    assert wire["apiTokenSet"] is True and "s3cret" not in json.dumps(wire)
    assert e.update(api_token="") == {"apiToken"} and e.api_headers() == {}


def test_settings_clamp_and_reset():
    s = Settings({"maxSolveTime": 9000, "autoSolve": 0, "statHands": "250", "bogus": 1})
    assert s["maxSolveTime"] == 600 and s["autoSolve"] is False and s["statHands"] == 250
    assert s.set("maxSolveTime", "7.5") and s["maxSolveTime"] == 7.5
    assert s.set("maxSolveTime", "") and s["maxSolveTime"] == DEFAULTS["maxSolveTime"]
    assert s.set("gateExploitability", 0) and s["gateExploitability"] == 0
    assert not s.set("nope", 1)
    assert s.reset() and s.to_wire() == DEFAULTS


def test_net_budget_is_optional_and_sent_only_when_set():
    from zigsolver_coordinator.solver_api import move_payload
    s = Settings({"maxNetSolveTime": 9000})
    assert s["maxNetSolveTime"] == 600
    assert s.set("maxNetSolveTime", "") and s["maxNetSolveTime"] is None
    body = {"body": {}, "maxSolveTime": 15}
    assert "maxNetSolveTime" not in move_payload({**body, "maxNetSolveTime": None})
    assert move_payload({**body, "maxNetSolveTime": 40})["maxNetSolveTime"] == 40


def test_regime_and_preflop_state_load_defensively():
    assert RegimeState("exploit").selected == "exploit"  # the pre-advanced format
    r = RegimeState({"selected": "nonsense", "exploitPct": 140, "requireStats": "yes"})
    assert (r.selected, r.exploit_pct, r.require_stats) == ("gto", 100, True)
    p = PreflopState({"selected": "advanced", "gtoPct": -3})
    assert (p.selected, p.gto_pct) == ("advanced", 0)
    assert p.resolve("gto", available=False)["forced"] == {"key": "reason.noPreflopService"}
    assert gto_available({"preflopEngines": ["alg", "gto"]}) and not gto_available({})


def test_the_pickers_are_kept_per_table():
    # A data directory from before the pickers went per table: its one choice is
    # where every table starts, so the upgrade changes nothing by itself.
    regime = PerTable(RegimeState, {"selected": "exploit", "exploitPct": 70, "requireStats": True})
    assert regime.of(3).to_wire() == regime.of(7).to_wire() == regime.template
    assert regime.of(3).select("manual") and regime.of(3).set_exploit_pct(20)
    assert regime.of(3).selected == "manual" and regime.of(3).exploit_pct == 20
    assert regime.of(7).selected == "exploit" and regime.of(7).exploit_pct == 70
    # Only a table that differs from where it started is written down.
    stored = json.loads(json.dumps(regime.to_store()))
    assert stored == {"selected": "exploit", "exploitPct": 70, "requireStats": True,
                      "tables": {"3": {"selected": "manual", "exploitPct": 20, "requireStats": True}}}
    again = PerTable(RegimeState, stored)
    assert again.of(3).selected == "manual" and again.of(7).selected == "exploit"
    # An older build reads the template, and ignores the tables beside it.
    assert RegimeState(stored).selected == "exploit"
    # The formats before that, and a stored table that is nonsense.
    assert PerTable(RegimeState, "exploit").of(1).selected == "exploit"
    odd = PerTable(RegimeState, {"tables": {"4": {"selected": "nope", "exploitPct": 140}, "5": 12}})
    assert odd.of(4).to_wire() == {"selected": "gto", "exploitPct": 100, "requireStats": True}
    assert odd.of(5).selected == "gto"
    preflop = PerTable(PreflopState, None)
    assert preflop.of(2).select("gto") and preflop.of(9).selected == "alg"
    assert preflop.to_store()["tables"] == {"2": {"selected": "gto", "gtoPct": 50}}


def test_exploit_availability_mirrors_the_endpoint():
    assert exploit_availability(None)["status"] == "pending"
    assert exploit_availability(parse_hand_body(FLOP_3WAY))["why"]["key"] == "reason.multiwayFlop"
    assert exploit_availability(parse_hand_body(RIVER_HU))["ok"]


def _preflop_body(hero_stack, villain_stack, villain_action="raise 2.2BB"):
    return (
        "Total pot 3.7BB\n\n"
        "u1\nposition=UTG\nfold\nstack=40BB\n\n"
        f"v\nposition=HJ\n{villain_action}\nstack={villain_stack}BB\n\n"
        f"*me*\nposition=CO\nstack={hero_stack}BB\nhand=A♠5♠\n\n"
        "btn\nposition=BTN\nwaiting\nstack=50BB\n\n"
        "sb\nposition=SB\nwaiting\nstack=18BB\n\n"
        "bb\nposition=BB\nwaiting\nstack=25BB"
    )


def test_preflop_exploit_is_the_allin_calculator_where_it_applies():
    # a short effective stack against the raiser: the calculator's spot
    short = exploit_availability(parse_hand_body(_preflop_body(22, 60)))
    assert short["status"] == "ok" and short["ok"]
    # the raiser is short even though the hero is deep: effective is vs the raiser
    assert exploit_availability(parse_hand_body(_preflop_body(80, 18)))["ok"]
    # deep both ways: the preflop engine's spot, not yet a regime question
    deep = exploit_availability(parse_hand_body(_preflop_body(80, 90)))
    assert deep["status"] == "pending" and not deep["ok"]
    assert deep["why"]["key"] == "reason.deepPreflop" and deep["why"]["count"] == 80
    # facing an all-in at any depth
    assert exploit_availability(parse_hand_body(_preflop_body(80, 0, "all-in 35BB")))["ok"]


def test_manual_stats_are_cleaned_capped_and_written():
    m = ManualStats()
    assert m.set(3, "Bob", "VPIP", "41.26")
    assert m.set(3, "Bob", "PFR", 150)
    assert m.table(3)["Bob"] == {"VPIP": 41.3, "PFR": 100}
    assert not m.set(3, "Bob", "AF", 2)          # not typeable
    assert m.to_wire(3)["Bob"]["lines"] == ["VPIP=41.3%", "PFR=100%"]
    assert m.set(3, "Bob", "VPIP", "") and m.set(3, "Bob", "PFR", None)
    assert m.table(3) == {}                      # nothing typed, nothing kept
    for i in range(MAX_NAMES + 5):
        m.set(4, f"p{i}", "VPIP", 10)
    assert len(m.table(4)) == MAX_NAMES and "p0" not in m.table(4)
    assert m.apply(9, FLOP_3WAY) == FLOP_3WAY    # nothing typed at table 9
    assert ManualStats(m.to_store()).tables == m.tables


def test_manual_tournament_rounds_and_phrases():
    t = ManualTournament()
    assert t.set(1, "playersLeft", "780.6")
    assert t.set(1, "averageStack", 78.94)
    assert not t.set(1, "playersPaid", 0)        # below the minimum: not a value
    assert t.values(1) == {"playersLeft": 781, "averageStack": 78.9}
    assert t.to_wire(1)["phrases"] == ["781 players left", "78.9BB average stack"]
    written = t.apply(1, FLOP_3WAY)
    assert parse_hand_body(written)["tournament"]["playersLeft"] == 781
    assert t.clear(1) and t.values(1) is None


def test_history_groups_replaces_and_rebuilds_ranges(tmp_path):
    h = History(Store(tmp_path))
    req = {"body": FLOP_3WAY, "regime": "gto", "handId": "t1#1-QsJs"}
    a1 = build_answer(answer(req), req)
    assert h.record_solve(1, "t1#1-QsJs", a1)
    # The same body in the same regime again replaces its row...
    assert h.record_solve(1, "t1#1-QsJs", build_answer(answer(req), req))
    assert h.count(1) == 1
    # ...a different regime on it is a different question.
    ex = {**req, "regime": "exploit"}
    h.record_solve(1, "t1#1-QsJs", build_answer(answer(ex), ex))
    assert h.count(1) == 2
    h.record_solve(1, "t1#1-QsJs", build_error("refused", request={**req, "body": RIVER_HU}))
    assert h.record_hand_end(1, "t1#1-QsJs", RIVER_HU + "\nHand finished")
    assert not h.record_hand_end(1, "t1#9-AhAd", RIVER_HU)  # never answered
    hands = h.hands_for(1)
    assert len(hands) == 1 and len(hands[0]["entries"]) == 3 and hands[0]["final"]
    first = hands[0]["entries"][0]
    assert [r["street"] for r in h.ranges_at(1, first)] == ["flop"]

    listed = h.list_wire(1)
    assert listed["decisions"] == 3
    assert listed["hands"][0]["entries"][2]["result"] == {
        "type": "error", "regime": None, "sampled": None, "request": {"regime": "gto"}}
    full = h.hand_wire(1, hands[0]["key"])
    assert full["record"]["finished"] and len(full["entries"]) == 3
    entry = h.entry_wire(1, first["id"])
    assert entry["hand"]["streetName"] == "flop" and entry["older"] is None and entry["newer"]


def test_history_persists_compactly_and_comes_back_whole(tmp_path):
    store = Store(tmp_path)
    h = History(store)
    req = {"body": FLOP_3WAY, "regime": "gto"}
    h.record_solve(2, "t2#1-QsJs", build_answer(answer(req), req))
    store.flush()
    on_disk = json.loads((tmp_path / "history" / "2.json").read_text())
    assert "meta" not in on_disk[0]["result"]       # payload.meta is written once
    again = History(Store(tmp_path))
    e = again.entries_for(2)[0]
    assert e["result"]["meta"]["ranges"]["players"][0]["name"] == "*me*"
    assert again.ranges_at(2, e)
    again.clear(2)
    store.flush()
    assert not (tmp_path / "history" / "2.json").exists()


def test_history_is_capped(tmp_path):
    h = History(Store(tmp_path))
    for i in range(PER_TABLE + 10):
        req = {"body": FLOP_3WAY + f"\n\nX{i}\nposition=SB\nfold\n", "regime": "gto"}
        h.record_solve(5, f"t5#{i}", build_answer(answer(req), req))
    assert h.count(5) == PER_TABLE


def test_state_file_is_private_and_atomic(tmp_path):
    store = Store(tmp_path)
    store.write_soon("state.json", lambda: {"config": {"apiToken": "x"}}, private=True)
    path = tmp_path / "state.json"
    assert json.loads(path.read_text())["config"]["apiToken"] == "x"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not (tmp_path / "state.json.tmp").exists()
    path.write_text("{ not json")
    assert store.read("state.json") is None
