# fuel-lake — tests for GeoParquet writing/reading and the lfmc_samples contract
# Licensed under Apache-2.0.
from __future__ import annotations

import io
import json
from datetime import date

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import shapely

from fuellake import tables
from fuellake.contracts import validate_table


def lfmc_frame(source="fems", day=date(2026, 6, 1), n=2) -> pd.DataFrame:
    rows = []
    for i in range(n):
        site, species = f"S{i}", "chamise"
        rows.append({
            "sample_id": tables.lfmc_sample_id(source, site, day, species), "source": source,
            "site_id": site, "site_name": f"Site {i}", "lon": -118.7 + i * 0.01, "lat": 34.1,
            "date": day, "species": species, "fuel_category": "shrub", "lfmc_pct": 75.0 + i,
            "n_raw_rows": 2, "outlier_flag": False, "qc_flags": "method_missing", "status": "Submitted",
            "ingested_at": pd.Timestamp("2026-11-14T20:00:00.123456789Z"),
        })
    df = pd.DataFrame(rows)
    df["n_raw_rows"] = df["n_raw_rows"].astype("int32")
    return df


def test_geoparquet_metadata_and_types(lake):
    rel = tables.table_part_path("lfmc_samples", "fems-lfmc", "2026-06/q:1")
    assert rel == "staged/tables/lfmc_samples/fems-lfmc/part-2026-06_q_1.parquet"
    tables.write_geoparquet(lake, rel, lfmc_frame())
    schema = pq.read_schema(io.BytesIO(lake.read_bytes(rel)))
    geo = json.loads(schema.metadata[b"geo"])
    assert geo["version"] == "1.1.0" and geo["primary_column"] == "geometry"
    col = geo["columns"]["geometry"]
    assert col["encoding"] == "WKB" and col["geometry_types"] == ["Point"]
    assert col["crs"]["id"] == {"authority": "EPSG", "code": 4326}
    assert str(schema.field("date").type) == "date32[day]"
    assert str(schema.field("ingested_at").type) == "timestamp[us, tz=UTC]"
    assert str(schema.field("n_raw_rows").type) == "int32"
    assert schema.names[:7] == ["sample_id", "source", "site_id", "site_name", "lon", "lat", "geometry"]
    res = validate_table(lake, rel, "lfmc_samples")
    assert res.ok, res.errors
    assert tables.parquet_n_rows(lake, rel) == 2


def test_read_table_across_parts_and_datasets(lake):
    tables.write_geoparquet(lake, tables.table_part_path("lfmc_samples", "fems-lfmc", "a"), lfmc_frame())
    tables.write_geoparquet(lake, tables.table_part_path("lfmc_samples", "fems-lfmc", "b"),
                            lfmc_frame(day=date(2026, 6, 2)))
    tables.write_geoparquet(lake, tables.table_part_path("lfmc_samples", "globe-lfmc", "all"),
                            lfmc_frame(source="globe-lfmc", n=3))
    df = tables.read_table(lake, "lfmc_samples")
    assert isinstance(df, gpd.GeoDataFrame) and len(df) == 7
    assert df.crs.to_epsg() == 4326 and df.geometry.iloc[0].geom_type == "Point"
    assert len(tables.read_table(lake, "lfmc_samples", "globe-lfmc")) == 3
    assert tables.read_table(lake, "nothing").empty


def test_read_table_drops_rows_repeated_across_aoi_parts(lake):
    # parts are written per AOI: a site inside two AOIs appears in both parts with the same sample_id
    older = lfmc_frame(n=2)
    newer = lfmc_frame(n=3).assign(ingested_at=pd.Timestamp("2026-11-15T08:00:00Z"), lfmc_pct=99.0)
    tables.write_geoparquet(lake, tables.table_part_path("lfmc_samples", "fems-lfmc", "smm__2026-06"), older)
    tables.write_geoparquet(lake, tables.table_part_path("lfmc_samples", "fems-lfmc", "smm2__2026-06"), newer)
    df = tables.read_table(lake, "lfmc_samples")
    assert len(df) == 3 and df["sample_id"].is_unique
    assert (df["lfmc_pct"] == 99.0).all()                         # the latest ingested_at wins
    assert isinstance(df, gpd.GeoDataFrame) and df.crs.to_epsg() == 4326
    assert len(tables.read_table(lake, "lfmc_samples", dedupe=False)) == 5


def test_polygon_geometry_column(lake):
    polys = [shapely.box(-118.8, 34.0, -118.7, 34.1), shapely.box(-118.6, 34.0, -118.5, 34.1)]
    df = pd.DataFrame({"fire_name": ["A", "B"], "geometry": polys})
    tables.write_geoparquet(lake, "staged/tables/fire_perimeters/fp/part-2025.parquet", df)
    back = tables.read_table(lake, "fire_perimeters")
    assert list(back.geometry.geom_type) == ["Polygon", "Polygon"]
    assert validate_table(lake, "staged/tables/fire_perimeters/fp/part-2025.parquet", "fire_perimeters").ok


def test_validate_table_catches_schema_problems(lake):
    df = lfmc_frame()
    dup = pd.concat([df, df], ignore_index=True)
    tables.write_geoparquet(lake, "t/dup.parquet", dup)
    assert any("duplicate" in e for e in validate_table(lake, "t/dup.parquet", "lfmc_samples").errors)
    bad = df.drop(columns=["qc_flags"]).assign(n_raw_rows=df["n_raw_rows"].astype("int64"),
                                               fuel_category="bush")
    tables.write_geoparquet(lake, "t/bad.parquet", bad)
    errors = validate_table(lake, "t/bad.parquet", "lfmc_samples").errors
    assert "missing column qc_flags" in errors
    assert any(e.startswith("column n_raw_rows has type int64") for e in errors)
    wrong_id = df.assign(sample_id="x")
    tables.write_geoparquet(lake, "t/id.parquet", wrong_id)
    assert any("sample_id" in e for e in validate_table(lake, "t/id.parquet", "lfmc_samples").errors)
    pd.DataFrame({"a": [1]}).to_parquet(lake.url("t/plain.parquet"))
    assert "missing GeoParquet 'geo' metadata" in validate_table(lake, "t/plain.parquet", "x").errors


def ignition_frame(ids=("a", "b"), ingested="2026-11-14T20:00:00Z", size=10.0) -> pd.DataFrame:
    return pd.DataFrame({"ignition_id": list(ids), "source": "fpa-fod", "fire_name": [f"F{i}" for i in ids],
                         "discovery_date": date(2024, 7, 1), "fire_size_ac": size,
                         "lon": -118.7, "lat": 34.1, "ingested_at": pd.Timestamp(ingested)})


def test_fire_ignitions_key_and_schema(lake):
    # parts per AOI (smm and ca) repeat ignitions inside both: read_table dedupes on ignition_id
    tables.write_geoparquet(lake, tables.table_part_path("fire_ignitions", "fire-ignitions", "smm__2024"),
                            ignition_frame())
    tables.write_geoparquet(lake, tables.table_part_path("fire_ignitions", "fire-ignitions", "ca__2024"),
                            ignition_frame(("a", "b", "c"), ingested="2026-11-15T08:00:00Z", size=20.0))
    df = tables.read_table(lake, "fire_ignitions")
    assert len(df) == 3 and df["ignition_id"].is_unique and (df["fire_size_ac"] == 20.0).all()
    assert len(tables.read_table(lake, "fire_ignitions", dedupe=False)) == 5
    rel = tables.table_part_path("fire_ignitions", "fire-ignitions", "ca__2024")
    res = validate_table(lake, rel, "fire_ignitions")
    assert res.ok and not res.warnings, (res.errors, res.warnings)
    tables.write_geoparquet(lake, "t/ign_dup.parquet", ignition_frame(("a", "a")))
    assert any("duplicate ignition_id" in e for e in validate_table(lake, "t/ign_dup.parquet", "fire_ignitions").errors)
    tables.write_geoparquet(lake, "t/ign_noid.parquet", ignition_frame().drop(columns=["ignition_id"]))
    assert "missing column ignition_id" in validate_table(lake, "t/ign_noid.parquet", "fire_ignitions").errors
