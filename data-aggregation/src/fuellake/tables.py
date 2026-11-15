# fuel-lake — GeoParquet table parts (write one part per source unit, read across parts)
# Licensed under Apache-2.0.
"""Tables live at ``staged/tables/<table>/<dataset>/part-<key>.parquet`` (GeoParquet 1.1, WKB, EPSG:4326).

``write_geoparquet`` builds the geometry column if it is missing: from ``lon``/``lat`` columns (points),
or from an existing ``geometry`` column of shapely objects / WKB bytes (e.g. fire perimeters).

``read_table`` returns a ``geopandas.GeoDataFrame`` (a ``pandas.DataFrame`` subclass) whose
``geometry`` column holds shapely geometries; an empty ``pandas.DataFrame`` if no part exists.

Parts are the files under ``staged/tables/<table>/`` **plus** the ``table`` assets of cataloged items in
collections whose ``fuellake:table`` is ``<table>``, wherever they live. That is how the gold table
``matchups`` (``features/<aoi>/matchups.parquet``, one file per AOI) is read.

Duplicates: connectors write one part per source unit **and AOI** (e.g. ``part-smm__2026-06``), so runs for
overlapping AOIs repeat the rows of sites inside both. ``read_table`` drops repeated rows on the table key
(:data:`TABLE_KEYS`: ``sample_id``, ``detection_id``, ``perimeter_id``, ``ignition_id``), keeping the latest
``ingested_at``, unless ``dedupe=False``. ``matchups`` and ``point_features`` have no key: each AOI's file is its
own sample set (rows differ by grid).
"""

from __future__ import annotations

import hashlib
import io
import re
from datetime import date

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import shapely

from fuellake.storage import Lake

TABLES_PREFIX = "staged/tables"
PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"

LFMC_COLUMNS = [
    "sample_id", "source", "site_id", "site_name", "lon", "lat", "geometry", "date", "species",
    "fuel_category", "lfmc_pct", "n_raw_rows", "outlier_flag", "qc_flags", "status", "ingested_at",
]
FUEL_CATEGORIES = ("shrub", "tree", "herb", "grass", "other", "unknown")
# Row identity per table, used by read_table(dedupe=True); see the module docstring.
TABLE_KEYS = {"lfmc_samples": "sample_id", "fire_detections": "detection_id", "fire_perimeters": "perimeter_id",
              "fire_ignitions": "ignition_id"}


def safe_key(key: str) -> str:
    """File-name-safe version of a source key."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(key)).strip("_") or "part"


def table_part_path(table: str, dataset: str, key: str) -> str:
    """Lake-relative path of one table part."""
    return f"{TABLES_PREFIX}/{table}/{dataset}/part-{safe_key(key)}.parquet"


def lfmc_sample_id(source: str, site_id: str, sample_date: date | str, species: str) -> str:
    """``sha1("source|site_id|date|species")`` as used for ``lfmc_samples.sample_id``."""
    d = sample_date.isoformat() if isinstance(sample_date, date) else str(sample_date)
    return hashlib.sha1(f"{source}|{site_id}|{d}|{species}".encode()).hexdigest()


def to_geodataframe(df: pd.DataFrame) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame in EPSG:4326 with a ``geometry`` column (see module docstring)."""
    if isinstance(df, gpd.GeoDataFrame) and df.geometry.name in df.columns:
        gdf = df.copy()
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        if gdf.geometry.name != "geometry":
            gdf = gdf.rename_geometry("geometry")
        return gdf
    if "geometry" in df.columns:
        geom = df["geometry"]
        if len(geom) and isinstance(geom.iloc[0], (bytes, bytearray, memoryview)):
            geom = shapely.from_wkb(geom.to_numpy())
        return gpd.GeoDataFrame(df.drop(columns=["geometry"]), geometry=gpd.GeoSeries(geom, index=df.index),
                                crs="EPSG:4326")
    if {"lon", "lat"} <= set(df.columns):
        return gpd.GeoDataFrame(df.copy(), geometry=gpd.points_from_xy(df["lon"], df["lat"]),
                                crs="EPSG:4326")
    raise ValueError("table needs lon/lat columns or a geometry column")


def write_geoparquet(lake: Lake, rel: str, df: pd.DataFrame) -> None:
    """Write ``df`` as GeoParquet (WKB geometry + ``geo`` metadata, EPSG:4326, zstd, µs timestamps)."""
    gdf = to_geodataframe(df)
    if "lon" in gdf.columns and "geometry" in gdf.columns:
        order = [c for c in LFMC_COLUMNS if c in gdf.columns]
        rest = [c for c in gdf.columns if c not in order]
        gdf = gdf[order + rest]
    buf = io.BytesIO()
    gdf.to_parquet(buf, index=False, compression="zstd", schema_version="1.1.0",
                   coerce_timestamps="us", allow_truncated_timestamps=True)
    lake.write_bytes(rel, buf.getvalue())


def read_parquet(lake: Lake, rel: str) -> pd.DataFrame:
    """Read one parquet file; GeoDataFrame if it carries GeoParquet metadata."""
    data = lake.read_bytes(rel)
    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
    if b"geo" in meta:
        return gpd.read_parquet(io.BytesIO(data))
    return pd.read_parquet(io.BytesIO(data))


def parquet_n_rows(lake: Lake, rel: str) -> int:
    with lake.open(rel, "rb") as f:
        return pq.ParquetFile(f).metadata.num_rows


def cataloged_parts(lake: Lake, table: str, dataset: str | None = None) -> list[str]:
    """Lake paths of the ``table`` assets of items in collections with ``fuellake:table == table``."""
    from fuellake import catalog

    out = []
    for coll in catalog.list_collections(lake):
        if coll.get("fuellake:table") != table or (dataset is not None and coll["id"] != dataset):
            continue
        for item in catalog.iter_items(lake, coll["id"]):
            if "table" in (item.get("assets") or {}):
                out.append(catalog.asset_rel(item, "table"))
    return out


def list_parts(lake: Lake, table: str, dataset: str | None = None) -> list[str]:
    """Lake-relative paths of all parts of ``table`` (optionally one dataset), sorted: the files under
    ``staged/tables/<table>/`` and the cataloged ``table`` assets elsewhere (see the module docstring)."""
    pattern = f"{TABLES_PREFIX}/{table}/{dataset or '*'}/part-*.parquet"
    parts = set(lake.glob(pattern)) | {p for p in cataloged_parts(lake, table, dataset) if lake.exists(p)}
    return sorted(parts)


def dedupe_rows(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Drop repeated rows on the key of ``table`` (:data:`TABLE_KEYS`), keeping the latest ``ingested_at``
    (else the last part in path order). Row order is otherwise kept. Tables without a key are returned as is."""
    key = TABLE_KEYS.get(table)
    if not key or key not in df.columns or not len(df) or not df[key].duplicated().any():
        return df
    order = df.sort_values("ingested_at", kind="stable", na_position="first") if "ingested_at" in df.columns else df
    keep = order.drop_duplicates(subset=[key], keep="last").index
    return df.loc[sorted(keep)].reset_index(drop=True)


def read_table(lake: Lake, table: str, dataset: str | None = None, *, dedupe: bool = True) -> pd.DataFrame:
    """Concatenate every part of ``table`` (optionally only ``dataset``'s parts).

    ``dedupe`` (default) drops rows repeated across parts on the table key, e.g. ``sample_id`` of
    ``lfmc_samples`` for sites inside two AOIs (see the module docstring)."""
    frames = [read_parquet(lake, p) for p in list_parts(lake, table, dataset)]
    frames = [f for f in frames if len(f)] or frames[:1]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if isinstance(frames[0], gpd.GeoDataFrame) and not isinstance(out, gpd.GeoDataFrame):
        out = gpd.GeoDataFrame(out, geometry="geometry", crs="EPSG:4326")
    return dedupe_rows(out, table) if dedupe else out
