# fuel-lake — example processing microservice: AOI statistics for every new item of a collection
# Licensed under Apache-2.0.
"""Example microservice built only on :class:`fuellake.client.LakeClient` (no lake paths, no core modules).

For each new or updated item of one collection (default ``nisar-sme2``) it computes the AOI mean and the
valid-pixel fraction of one raster asset and upserts a row into ``<out>/aoi_stats.parquet``. The
checkpoint ``<out>/checkpoint.json`` makes it process each item version (``fuellake:run_id``) exactly once:
the first run backfills from the catalog, later runs follow the event log.

Run: ``uv run python -m fuellake.services.example_consumer --out ./consumer-out [--once]``.
Which lake: ``--api-url`` / ``FUELLAKE_API_URL`` (HTTP API), else ``LAKE_ROOT`` (direct). Other settings:
``FUELLAKE_CONSUMER_OUT``, ``FUELLAKE_CONSUMER_COLLECTION``, ``FUELLAKE_CONSUMER_ASSET``,
``FUELLAKE_CONSUMER_INTERVAL`` (seconds between polls).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from fuellake.client import LakeClient

ITEM_EVENTS = ("item.created", "item.updated")


def item_stats(client: LakeClient, item: dict, asset: str | None = None) -> dict:
    """AOI mean and valid-pixel fraction of one raster asset (default: the first data asset)."""
    key = asset or next(k for k, a in item["assets"].items() if "data" in (a.get("roles") or []))
    da = client.open_asset(item, key)                     # 2-D (y, x) on the AOI grid
    arr = np.asarray(da.values, dtype="float64")
    nodata = da.rio.nodata
    valid = np.isfinite(arr) if nodata is None or math.isnan(nodata) else np.isfinite(arr) & (arr != nodata)
    props = item["properties"]
    return {
        "item_id": item["id"], "collection": item["collection"], "datetime": props["datetime"],
        "aoi": props.get("fuellake:aoi"), "asset": key, "maturity": props.get("fuellake:maturity"),
        "mean": float(arr[valid].mean()) if valid.any() else None,          # JSON/parquet-safe (no NaN)
        "valid_fraction": float(valid.mean()), "n_pixels": int(arr.size),
        "version": props.get("fuellake:run_id"), "processed_at": datetime.now(UTC).isoformat(),
    }


def run_once(client: LakeClient, collection: str, out_dir: str | Path, asset: str | None = None) -> list[dict]:
    """One polling cycle. Returns the rows computed in this cycle (empty if nothing was new)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ckpt = out / "checkpoint.json"
    state = json.loads(ckpt.read_text()) if ckpt.exists() else {"cursor": None, "done": {}}
    evs = client.events(since=state["cursor"], types=ITEM_EVENTS)    # read before the backfill: no gap
    if state["cursor"] is None:
        # Per-item statistics are not a time series, so mixed NISAR maturities are fine here; the
        # maturity is recorded in each row for downstream filtering.
        items = client.search(collections=[collection], allow_mixed_maturity=True)
    else:
        ids = sorted({e["item_id"] for e in evs if e.get("collection") == collection})  # since is inclusive
        items = [it for it in (client.get_item(collection, i) for i in ids) if it is not None]
    done = state["done"]
    todo = [it for it in items if it["id"] not in done or done[it["id"]] != it["properties"].get("fuellake:run_id")]
    rows = [item_stats(client, it, asset) for it in todo]
    if rows:
        results = out / "aoi_stats.parquet"
        df = pd.DataFrame(rows)
        if results.exists():
            old = pd.read_parquet(results)
            df = pd.concat([old[~old["item_id"].isin(df["item_id"])], df], ignore_index=True)
        df.sort_values(["datetime", "item_id"]).to_parquet(results, index=False)
    for row in rows:                                          # checkpoint only after the results are written
        state["done"][row["item_id"]] = row["version"]
    if evs:
        state["cursor"] = evs[-1]["time"]
    tmp = ckpt.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(ckpt)                                         # atomic on POSIX
    return rows


def main(argv: list[str] | None = None) -> None:
    env = os.environ.get
    p = argparse.ArgumentParser(description="fuel-lake example microservice: AOI stats per new item.")
    p.add_argument("--collection", default=env("FUELLAKE_CONSUMER_COLLECTION", "nisar-sme2"))
    p.add_argument("--asset", default=env("FUELLAKE_CONSUMER_ASSET"), help="asset key (default: first data asset)")
    p.add_argument("--out", default=env("FUELLAKE_CONSUMER_OUT", "consumer-out"), help="output directory")
    p.add_argument("--api-url", default=None, help="HTTP API URL (default: $FUELLAKE_API_URL, else $LAKE_ROOT)")
    p.add_argument("--interval", type=float, default=float(env("FUELLAKE_CONSUMER_INTERVAL", "60")))
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = p.parse_args(argv)
    with LakeClient.from_env(api_url=args.api_url) as client:
        while True:
            rows = run_once(client, args.collection, args.out, args.asset)
            print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {args.collection}: processed {len(rows)} item(s)",
                  flush=True)
            if args.once:
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
