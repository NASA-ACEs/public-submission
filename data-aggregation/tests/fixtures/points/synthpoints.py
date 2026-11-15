# fuel-lake — synthetic lake for the point-feature tests (two AOIs, point tables, weather, SME2, statics, S2)
# Licensed under Apache-2.0.
"""Offline lake for :mod:`fuellake.features.points` tests, built on :mod:`fixtures.features.synthlake`.

AOIs
  tiny   30 m EPSG:32611 local AOI (``synthlake.TINY_AOI``): the statics are staged here.
  wide   region-scale AOI on the EASE-Grid 2.0 200 m reference grid around ``tiny`` (the build AOI).
Datasets (test config dir)
  lfmc-test         table lfmc_samples
  ign-test          table fire_ignitions (columns of fuellake.connectors.fire_ignitions.IGNITION_SCHEMA)
  wx-test           raster-b t2m, swvl1, th (synthlake)
  wx-alias          raster-b base_sm and team_sm (alias_of: base_sm, lag: true)
  nisar-sme2        raster-a dynamic soil_moisture (PROVISIONAL), scales local + region
  smap-climatology  raster-b, 366 steps of 1904: l4_sm_surface_mean / _std
  landfire-fbfm40   static fuel_model, options.releases LF 2016 / LF 2022 / LF 2025 (fake services)
  landfire-topo     static elevation / slope / aspect (remote only in the tests)
  cop-dem           static elevation / slope / aspect
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from fixtures.features.synthlake import DATASETS as FEATURE_DATASETS
from fixtures.features.synthlake import TINY_AOI, SynthLake, _ds, _var
from fuellake.connectors.base import StagedAsset
from fuellake.tables import PARQUET_MEDIA_TYPE, table_part_path, write_geoparquet

WIDE_AOI = {
    "id": "wide",
    "name": "Wide test AOI on the SME2 grid",
    "bbox": [-118.90, 34.00, -118.70, 34.15],
    "reference_grid": "ease2-200m",
    "scale": "region",
}

RELEASES = [
    {"version": "LF 2016", "year": 2016, "image_service": "https://example.invalid/LF2016_FBFM40/ImageServer",
     "disturbances": {"first_year": 2010, "last_year": 2016, "through": None}},
    {"version": "LF 2022", "year": 2022, "image_service": "https://example.invalid/LF2022_FBFM40/ImageServer",
     "disturbances": {"first_year": 2013, "last_year": 2022, "through": "2022-09-30"}},
    {"version": "LF 2025", "year": 2025, "image_service": "https://example.invalid/LF2025_FBFM40/ImageServer",
     "disturbances": {"first_year": 2016, "last_year": 2025, "through": "2025-09-30"}},
]

TERRAIN_VARS = [_var("elevation", "m"), _var("slope", "degree"), _var("aspect", "degree", resampling="nearest")]

DATASETS = {
    "lfmc-test": FEATURE_DATASETS["lfmc-test"],
    "wx-test": FEATURE_DATASETS["wx-test"],
    "ign-test": _ds("ign-test", "table", table="fire_ignitions", scales=["local", "region"]),
    "wx-alias": _ds("wx-alias", "raster-b", [_var("base_sm", "m3/m3"),
                                             {**_var("team_sm", "m3/m3"), "alias_of": "base_sm", "lag": True}],
                    cadence="daily", scales=["local", "region"]),
    "nisar-sme2": _ds("nisar-sme2", "raster-a", [_var("soil_moisture", "m3/m3", valid_range=[0.0, 0.7])],
                      maturity="PROVISIONAL", cadence="12 days", scales=["local", "region"]),
    "smap-climatology": _ds("smap-climatology", "raster-b",
                            [_var("l4_sm_surface_mean", "m3/m3"), _var("l4_sm_surface_std", "m3/m3")],
                            cadence="static (day-of-year climatology)", scales=["local", "region"],
                            options={"reference_year": 1904}),
    "landfire-fbfm40": _ds("landfire-fbfm40", "raster-a",
                           [_var("fuel_model", None, dtype="int16", nodata=-9999, resampling="nearest")],
                           cadence="static (releases)", options={"releases": RELEASES}),
    "landfire-topo": _ds("landfire-topo", "raster-a", TERRAIN_VARS, cadence="static (LF 2020)",
                         options={"product_year": 2020, "product_version": "LF 2020"}),
    "cop-dem": _ds("cop-dem", "raster-a", TERRAIN_VARS, cadence="static (2021)"),
}


def write_config(root: Path) -> Path:
    (root / "aois").mkdir(parents=True, exist_ok=True)
    (root / "datasets").mkdir(exist_ok=True)
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    (root / "aois" / "wide.yaml").write_text(yaml.safe_dump(WIDE_AOI))
    for ds_id, d in DATASETS.items():
        (root / "datasets" / f"{ds_id}.yaml").write_text(yaml.safe_dump(d))
    return root


def ignitions(sl: SynthLake, rows: list[dict], key: str = "part1", ds_id: str = "ign-test") -> dict:
    """One ``fire_ignitions`` part; each row needs ``ignition_id``, ``lon``, ``lat``, ``discovery_date``."""
    full = []
    for r in rows:
        size = r.get("fire_size_ha")
        d = {"source": "fpa-fod", "source_id": r["ignition_id"], "fire_name": f"F{r['ignition_id']}",
             "fire_year": r["discovery_date"].year, "fire_size_ha": size,
             "fire_size_ac": None if size is None else size / 0.40468564224, "size_class": "B",
             "cause_class": "human", "cause_general": "Debris", "mtbs_id": None, "irwin_id": None,
             "discovery_datetime": pd.Timestamp(datetime.combine(r["discovery_date"], time(20), tzinfo=UTC)),
             "license": "test", "ingested_at": pd.Timestamp("2026-11-14", tz="UTC"), **r}
        full.append(d)
    df = pd.DataFrame(full)
    df["fire_size_ha"] = df["fire_size_ha"].astype("float64")
    df["fire_size_ac"] = df["fire_size_ac"].astype("float64")
    path = table_part_path("fire_ignitions", ds_id, key)
    write_geoparquet(sl.lake, path, df)
    day = min(df["discovery_date"])
    t0 = datetime.combine(day, time.min, tzinfo=UTC)
    return sl._register(ds_id, f"{ds_id}__{sl.aoi.id}__{day:%Y%m%d}__{key}", t0,
                        [StagedAsset("table", path, PARQUET_MEDIA_TYPE, ["data"])], {},
                        bbox=(float(df.lon.min()), float(df.lat.min()), float(df.lon.max()), float(df.lat.max())))


def climatology(sl: SynthLake, mean_fn, std_fn) -> dict:
    """A ``smap-climatology`` item: 366 daily steps of 1904; ``mean_fn(step)`` / ``std_fn(step)`` (0-based)."""
    return sl.weather("smap-climatology", date(1904, 1, 1), 366,
                      {"l4_sm_surface_mean": lambda k, lon, lat: mean_fn(k) + 0 * lon,
                       "l4_sm_surface_std": lambda k, lon, lat: std_fn(k) + 0 * lon})


def lf_release(sl: SynthLake, year: int, code: int) -> dict:
    """A staged LANDFIRE FBFM40 release item (constant code) on the SynthLake's AOI."""
    return sl.raster("landfire-fbfm40", date(year, 1, 1), {"fuel_model": sl.const(code, "int16")},
                     discriminator=f"lf{year}")


# ---------------------------------------------------------------------------- Sentinel-2 like COGs
S2_SCALE, S2_OFFSET = 1e-4, -0.1


def s2_scene(tmp: Path, scene_id: str, when: date, centre_xy: tuple[float, float], dn: dict[str, int],
             scl_value: int = 4, cloud_at_centre: bool = False) -> dict:
    """Small EPSG:32611 band files (10 m red/nir, 20 m nir08/swir16/scl) around ``centre_xy`` and a STAC-like
    item dict pointing at them. ``dn``: constant DN per band key."""
    import rasterio
    from affine import Affine
    from pyproj import Transformer

    cx, cy = centre_xy
    out = tmp / scene_id
    out.mkdir(parents=True, exist_ok=True)
    assets = {}
    for key, res in (("red", 10), ("nir", 10), ("nir08", 20), ("swir16", 20), ("scl", 20)):
        size = 600 // res
        x0, y0 = cx - 300 - (cx % res), cy + 300 - (cy % res)
        transform = Affine(res, 0, x0, 0, -res, y0)
        if key == "scl":
            arr = np.full((size, size), scl_value, dtype="uint8")
            if cloud_at_centre:
                c, r = ~transform @ (cx, cy)
                arr[int(r) - 1 : int(r) + 2, int(c) - 1 : int(c) + 2] = 9
            dtype = "uint8"
        else:
            arr = np.full((size, size), dn[key], dtype="uint16")
            dtype = "uint16"
        path = out / f"{key}.tif"
        with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1, dtype=dtype,
                           crs="EPSG:32611", transform=transform, nodata=0) as dst:
            dst.write(arr, 1)
        assets[key] = {"href": str(path), "raster:bands": [{"scale": S2_SCALE, "offset": S2_OFFSET}]}
    tr = Transformer.from_crs("EPSG:32611", "EPSG:4326", always_xy=True)
    ring = [tr.transform(x, y) for x, y in ((cx - 280, cy - 280), (cx + 280, cy - 280), (cx + 280, cy + 280),
                                             (cx - 280, cy + 280), (cx - 280, cy - 280))]
    return {"id": scene_id, "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
            "properties": {"datetime": f"{when.isoformat()}T18:40:00Z", "s2:processing_baseline": "05.11",
                           "proj:epsg": 32611, "mgrs:utm_zone": 11, "mgrs:latitude_band": "S",
                           "mgrs:grid_square": "LT"},
            "assets": assets}
