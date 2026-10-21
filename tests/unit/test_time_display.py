from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from engine.data_layer.futures_pipeline import _timestamp


IST = ZoneInfo("Asia/Kolkata")


def test_utc_to_ist_conversion_crosses_midnight_without_double_conversion():
    source = datetime(2026, 1, 1, 18, 45, tzinfo=timezone.utc)
    displayed = source.astimezone(IST)
    assert displayed.isoformat() == "2026-01-02T00:15:00+05:30"
    assert _timestamp(str(int(source.timestamp()))) == source.isoformat()


def test_ist_source_timestamp_is_preserved_as_its_instant():
    assert _timestamp("2026-01-02T00:15:00+05:30") == "2026-01-01T18:45:00+00:00"
