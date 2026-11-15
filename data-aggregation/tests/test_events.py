# fuel-lake — tests for the append-only JSONL event log
# Licensed under Apache-2.0.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fuellake import events


def test_emit_and_read_since(lake):
    t = lambda d, h: datetime(2026, 11, d, h, tzinfo=UTC)  # noqa: E731
    events.emit(lake, "item.created", dataset="a", item_id="1", time=t(13, 23))
    events.emit(lake, "item.updated", dataset="a", item_id="1", time=t(14, 8))
    events.emit(lake, "run.completed", run_id="r1", time=t(14, 20), counts={"items": 1})
    assert lake.ls("events") == ["events/2026-11-13.jsonl", "events/2026-11-14.jsonl"]
    assert len(events.read_events(lake)) == 3
    since = events.read_events(lake, since=t(14, 8))
    assert [e["type"] for e in since] == ["item.updated", "run.completed"]  # inclusive
    assert events.read_events(lake, since="2026-11-14T08:00:00.000001Z")[0]["type"] == "run.completed"
    assert [e["type"] for e in events.read_events(lake, types=["item.created"])] == ["item.created"]
    ev = since[-1]
    assert ev["time"] == "2026-11-14T20:00:00.000000Z" and ev["counts"] == {"items": 1}
    assert set(ev) >= {"event_id", "time", "type", "dataset", "aoi", "collection", "item_id", "href", "run_id"}


def test_unknown_event_type(lake):
    with pytest.raises(ValueError):
        events.emit(lake, "item.deleted")
