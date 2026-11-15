# fuel-lake — append-only JSONL event log (events/<yyyy-mm-dd>.jsonl)
# Licensed under Apache-2.0.
"""Event log for consumers that react to new data.

Each event is one JSON line in ``events/<yyyy-mm-dd>.jsonl`` (UTC date of the event)::

    {"event_id": "<uuid4>", "time": "2026-11-14T20:00:00.123456Z", "type": "item.created",
     "dataset": "nisar-sme2", "aoi": "smm", "collection": "nisar-sme2", "item_id": "...",
     "href": "catalog/nisar-sme2/<item_id>/<item_id>.json", "run_id": "..."}

Types: ``item.created``, ``item.updated``, ``table.updated``, ``run.completed``, ``run.failed``.
``href`` is lake-relative. ``read_events(since=...)`` is **inclusive** (``time >= since``); de-duplicate
on ``event_id`` when polling.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

from fuellake.catalog import to_utc
from fuellake.storage import Lake

EVENT_TYPES = ("item.created", "item.updated", "table.updated", "run.completed", "run.failed")
EVENTS_DIR = "events"


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def emit(lake: Lake, type: str, *, dataset: str | None = None, aoi: str | None = None,
         collection: str | None = None, item_id: str | None = None, href: str | None = None,
         run_id: str | None = None, time: datetime | None = None, **extra: Any) -> dict:
    """Append one event and return it. Extra keyword fields are stored as-is (must be JSON-safe)."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {type!r}; expected one of {EVENT_TYPES}")
    when = (time or _now()).astimezone(UTC)
    event = {
        "event_id": str(uuid.uuid4()),
        "time": _fmt(when),
        "type": type,
        "dataset": dataset,
        "aoi": aoi,
        "collection": collection,
        "item_id": item_id,
        "href": href,
        "run_id": run_id,
        **extra,
    }
    line = json.dumps(event, allow_nan=False, ensure_ascii=False) + "\n"
    lake.append_text(f"{EVENTS_DIR}/{when:%Y-%m-%d}.jsonl", line)
    return event


def read_events(lake: Lake, since: datetime | date | str | None = None,
                types: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    """Events with ``time >= since`` (all if ``since`` is None), oldest first."""
    t0 = to_utc(since) if since is not None else None
    files = lake.glob(f"{EVENTS_DIR}/*.jsonl")
    if t0 is not None:
        first_day = t0.date().isoformat()
        files = [p for p in files if p.rsplit("/", 1)[-1][:10] >= first_day]
    out = []
    for rel in sorted(files):
        for line in lake.read_text(rel).splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if t0 is not None and to_utc(ev["time"]) < t0:
                continue
            if types and ev.get("type") not in types:
                continue
            out.append(ev)
    out.sort(key=lambda e: e["time"])
    return out
