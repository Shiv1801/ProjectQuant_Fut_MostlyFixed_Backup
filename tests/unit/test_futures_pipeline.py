from datetime import datetime, timezone

from config import db
from engine.data_layer.futures_pipeline import (
    build_live_futures_candle,
    build_continuous_futures_candles,
    normalize_futures_candles,
    normalize_futures_depth,
    parse_futures_quotes,
)
from engine.signal_layer.momentum.ofi import discover_nifty_monthly_futures
from engine.shared_state.store import SharedState


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def epoch(year, month, day):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def master_records():
    return [
        {"symTicker": "NSE:NIFTY26NOVFUT", "expiryDate": epoch(2026, 11, 26)},
        {"symTicker": "NSE:NIFTY26OCTFUT", "expiryDate": epoch(2026, 10, 29)},
        {"symTicker": "NSE:NIFTY26DECFUT", "expiryDate": epoch(2026, 12, 31)},
        {"symTicker": "NSE:NIFTY26OCT29000CE", "expiryDate": epoch(2026, 10, 29)},
        {"symTicker": "NSE:NIFTYBANK26OCTFUT", "expiryDate": epoch(2026, 10, 29)},
        {"symTicker": "NSE:NIFTY26SEPFUT", "expiryDate": epoch(2026, 9, 24)},
    ]


def contracts():
    return discover_nifty_monthly_futures(master_records(), now=NOW)


def test_selects_current_and_next_by_actual_expiry():
    selected = contracts()
    assert [(item["role"], item["symbol"]) for item in selected] == [
        ("current_month", "NSE:NIFTY26OCTFUT"),
        ("next_month", "NSE:NIFTY26NOVFUT"),
    ]


def test_rollover_automatically_changes_current_contract():
    selected = discover_nifty_monthly_futures(
        master_records(), now=datetime(2026, 10, 30, tzinfo=timezone.utc)
    )
    assert selected[0]["symbol"] == "NSE:NIFTY26NOVFUT"
    assert selected[0]["role"] == "current_month"
    assert selected[1]["symbol"] == "NSE:NIFTY26DECFUT"


def test_expired_or_malformed_master_does_not_produce_contracts():
    assert discover_nifty_monthly_futures(
        [{"symTicker": "NSE:NIFTY26OCTFUT", "expiryDate": epoch(2026, 10, 1)}],
        now=NOW,
    ) == []
    assert discover_nifty_monthly_futures({"unexpected": "shape"}, now=NOW) == []


def test_quote_validation_preserves_role_and_rejects_missing_timestamp():
    selected = contracts()
    raw = {
        "s": "ok",
        "d": [
            {"n": selected[0]["symbol"], "v": {"lp": 25000, "tt": epoch(2026, 10, 15), "ch": 5, "chp": 0.02}},
            {"n": selected[1]["symbol"], "v": {"lp": 25100}},
        ],
    }
    parsed = parse_futures_quotes(raw, selected, "2026-10-15T10:00:00+05:30")
    assert parsed["current_month"]["status"] == "valid"
    assert parsed["current_month"]["symbol"] == selected[0]["symbol"]
    assert parsed["next_month"]["status"] == "invalid_timestamp"


def test_quote_validation_accepts_numeric_string_timestamp_from_fyers():
    selected = contracts()
    raw = {
        "s": "ok",
        "d": [{"n": selected[0]["symbol"], "v": {"lp": 25000, "tt": str(epoch(2026, 10, 15))}}],
    }
    parsed = parse_futures_quotes(raw, selected, "2026-10-15T10:00:00+05:30")
    assert parsed["current_month"]["status"] == "valid"


def test_live_quote_updates_only_the_current_minute_without_inventing_volume():
    contract = contracts()[0]
    existing = {
        "minute_bucket": "2026-10-16 04:00:00", "open": 100, "high": 101,
        "low": 99, "close": 100.5, "volume": 25, "volume_status": "valid",
    }
    quote = {"ltp": 102, "fetched_at": "2026-10-16T09:30:45+05:30"}
    candle = build_live_futures_candle(quote, contract, existing)
    assert candle["minute_bucket"] == "2026-10-16 04:00:00"
    assert candle["open"] == 100
    assert candle["high"] == 102
    assert candle["low"] == 99
    assert candle["close"] == 102
    assert candle["volume"] == 25

    next_candle = build_live_futures_candle(
        {"ltp": 103, "fetched_at": "2026-10-16T09:31:01+05:30"}, contract, candle
    )
    assert next_candle["minute_bucket"] == "2026-10-16 04:01:00"
    assert next_candle["open"] == 103
    assert next_candle["volume"] is None
    assert next_candle["volume_status"] == "unavailable"


def test_candle_volume_status_distinguishes_zero_and_missing():
    selected = contracts()[0]
    raw = {"s": "ok", "candles": [
        [epoch(2026, 10, 15), 1, 2, 0.5, 1.5, 0],
        [epoch(2026, 10, 15) + 60, 1.5, 2.5, 1, 2, None],
    ]}
    normalized = normalize_futures_candles(raw, selected)
    assert [item["volume_status"] for item in normalized] == ["zero", "missing"]
    assert all(item["symbol"] == selected["symbol"] for item in normalized)


def test_candle_normalization_orders_and_deduplicates_valid_ohlc():
    selected = contracts()[0]
    raw = {"s": "ok", "candles": [
        [epoch(2026, 10, 15) + 60, 2, 3, 1, 2.5, 10],
        [epoch(2026, 10, 15), 1, 2, 0.5, 1.5, 8],
        [epoch(2026, 10, 15) + 60, 99, 100, 98, 99, 20],
        [epoch(2026, 10, 15) + 120, 5, 4, 3, 4, 10],
    ]}
    normalized = normalize_futures_candles(raw, selected)
    assert [item["minute_bucket"] for item in normalized] == [
        "2026-10-15 00:00:00", "2026-10-15 00:01:00",
    ]
    assert normalized[-1]["open"] == 99


def test_continuous_candles_roll_by_expiry_and_back_adjust_prices():
    rows = [
        {"symbol": "NSE:NIFTY26OCTFUT", "expiry_epoch": epoch(2026, 10, 29), "minute_bucket": "2026-10-29 09:29:00", "candle_timestamp": "2026-10-29T09:29:00+00:00", "open": 100, "high": 102, "low": 99, "close": 101},
        {"symbol": "NSE:NIFTY26NOVFUT", "expiry_epoch": epoch(2026, 11, 26), "minute_bucket": "2026-10-29 09:30:00", "candle_timestamp": "2026-10-29T09:30:00+00:00", "open": 105, "high": 107, "low": 104, "close": 106},
    ]
    continuous = build_continuous_futures_candles(rows)
    assert [row["contract_symbol"] for row in continuous] == [
        "NSE:NIFTY26OCTFUT", "NSE:NIFTY26NOVFUT"
    ]
    assert continuous[1]["open"] == continuous[0]["close"]
    assert continuous[1]["close"] == 102
    assert continuous[1]["symbol"] == "NSE:NIFTY_CONTINUOUS_FUT"


def test_depth_validation_distinguishes_zero_from_incomplete():
    selected = contracts()[0]
    zero = normalize_futures_depth(
        {"s": "ok", "d": {selected["symbol"]: {"totalbuyqty": 0, "totalsellqty": 0}}},
        selected,
        "2026-10-15T10:00:00+05:30",
    )
    incomplete = normalize_futures_depth(
        {"s": "ok", "d": {selected["symbol"]: {"totalbuyqty": 1}}},
        selected,
        "2026-10-15T10:00:00+05:30",
    )
    assert zero["status"] == "valid"
    assert zero["depth_status"] == "no_depth"
    assert incomplete["status"] == "incomplete"


def test_shared_state_keeps_current_and_next_data_isolated():
    state = SharedState()
    selected = contracts()
    state.set_futures_contracts(selected)
    state.set_futures_quote("current_month", {"status": "valid", "symbol": selected[0]["symbol"], "fetched_at": "now"})
    state.set_futures_quote("next_month", {"status": "valid", "symbol": selected[1]["symbol"], "fetched_at": "later"})
    assert state.futures_quotes["current_month"]["symbol"] != state.futures_quotes["next_month"]["symbol"]
    state.mark_futures_discovery_unavailable("symbol master unavailable")
    assert state.futures_contracts == []
    assert [item["symbol"] for item in state.futures_contracts_last_known] == [
        item["symbol"] for item in selected
    ]
    assert state.futures_discovery_status == "unavailable"


def test_futures_persistence_is_contract_aware_and_deduplicated(tmp_path, monkeypatch):
    database_path = tmp_path / "futures.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    db.init_db()
    selected = contracts()
    quote = {
        "role": "current_month", "symbol": selected[0]["symbol"],
        "expiry_epoch": selected[0]["expiry_epoch"],
        "quote_timestamp": "2026-10-15T10:00:00+00:00", "fetched_at": "now",
        "ltp": 25000,
    }
    assert db.insert_futures_quote(quote) is True
    assert db.insert_futures_quote(quote) is False
    candles = [{
        "role": role, "symbol": item["symbol"], "expiry_epoch": item["expiry_epoch"],
        "minute_bucket": "2026-10-15 10:00:00", "candle_timestamp": "2026-10-15T10:00:00+00:00",
        "open": 1, "high": 2, "low": 0.5, "close": 1.5,
        "volume": 10, "volume_status": "valid",
    } for role, item in (("current_month", selected[0]), ("next_month", selected[1]))]
    assert db.insert_futures_candles(candles) == 2
    assert len(db.get_recent_futures_candles(selected[0]["symbol"])) == 1
    depth = {
        "role": "current_month", "symbol": selected[0]["symbol"],
        "expiry_epoch": selected[0]["expiry_epoch"],
        "observed_at": "2026-10-15T10:00:00+00:00", "status": "valid",
        "depth_status": "valid", "total_buy_qty": 10, "total_sell_qty": 5,
        "raw": {"s": "ok"},
    }
    assert db.insert_futures_depth_snapshot(depth) is True
    assert db.insert_futures_depth_snapshot(depth) is False


def test_futures_poller_start_is_idempotent(monkeypatch):
    from engine.background import pollers

    class FakeTask:
        def done(self):
            return False

    created = []

    def fake_create_task(coroutine):
        coroutine.close()
        task = FakeTask()
        created.append(task)
        return task

    original_task = pollers._futures_depth_task
    pollers._futures_depth_task = None
    monkeypatch.setattr(pollers.asyncio, "create_task", fake_create_task)
    try:
        first = pollers.start_futures_depth_poller()
        second = pollers.start_futures_depth_poller()
        assert first is second
        assert len(created) == 1
    finally:
        pollers._futures_depth_task = original_task
