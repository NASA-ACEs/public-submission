# fuel-lake — build config/regions/<id>.geojson from a US Census cartographic boundary file (one state)
# Licensed under Apache-2.0.
"""Extract one state from a Census Bureau cartographic boundary shapefile and write it as the lake region.

Usage::

    uv run python scripts/build_region.py                      # download cb_2025_us_state_500k.zip, California
    uv run python scripts/build_region.py --zip /path/cb_2025_us_state_500k.zip --statefp 06 \\
        --out config/regions/california.geojson [--simplify-m 0]

Steps: read the shapefile (NAD83, EPSG:4269) straight from the zip, keep the feature with ``STATEFP``, transform
it to EPSG:4326, optionally simplify it in EPSG:3310 (``--simplify-m``, Douglas-Peucker with topology
preserved), refuse a result whose Hausdorff distance to the source exceeds ``--max-error-m`` (default 500 m),
round coordinates to 6 decimals (about 0.1 m) and write an RFC 7946 FeatureCollection with one feature.

The default (``--simplify-m 0``) keeps every vertex: the 1:500,000 California outline has 5,135 vertices, so
simplification is not needed. The printed statistics (vertices, parts, area, Hausdorff error, md5 of the zip)
belong in ``config/regions/<id>.yaml``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

DEFAULT_URL = "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip"


def _download(url: str, dst: Path) -> None:
    import requests

    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "fuel-lake/0.1"}) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for block in r.iter_content(1 << 20):
                f.write(block)


def build(zip_path: Path, statefp: str, simplify_m: float, max_error_m: float) -> tuple[dict, dict]:
    import geopandas as gpd
    import shapely
    from shapely.geometry import mapping

    gdf = gpd.read_file(f"zip://{zip_path}")
    sel = gdf[gdf["STATEFP"] == statefp]
    if len(sel) != 1:
        raise SystemExit(f"expected one feature with STATEFP={statefp}, found {len(sel)}")
    row = sel.iloc[0]
    src_crs = gdf.crs.to_string()
    metric = gpd.GeoSeries([row.geometry], crs=gdf.crs).to_crs("EPSG:3310").iloc[0]
    out_metric = shapely.simplify(metric, simplify_m, preserve_topology=True) if simplify_m > 0 else metric
    error_m = float(metric.hausdorff_distance(out_metric))
    if error_m > max_error_m:
        raise SystemExit(f"simplification error {error_m:.0f} m exceeds --max-error-m {max_error_m:.0f}")
    geom = gpd.GeoSeries([out_metric], crs="EPSG:3310").to_crs("EPSG:4326").iloc[0]
    geom = shapely.set_precision(geom, 1e-6)
    geom = shapely.make_valid(geom) if not geom.is_valid else geom
    stats = {
        "name": str(row["NAME"]), "stusps": str(row["STUSPS"]), "geoid": str(row["GEOID"]),
        "source_crs": src_crs, "parts": len(getattr(geom, "geoms", [geom])),
        "vertices": int(shapely.get_num_coordinates(geom)),
        "area_km2": round(float(gpd.GeoSeries([geom], crs="EPSG:4326").to_crs("EPSG:3310").area.iloc[0]) / 1e6, 1),
        "bbox": [round(v, 6) for v in geom.bounds], "simplify_m": simplify_m,
        "hausdorff_error_m": round(error_m, 1), "valid": bool(geom.is_valid),
    }
    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"STATEFP": statefp, "GEOID": stats["geoid"], "STUSPS": stats["stusps"],
                           "NAME": stats["name"],
                           "description": "fuel-lake monitored region; see the YAML file of the same name for "
                                          "source, vintage and licence"},
            "geometry": mapping(geom),
        }],
    }
    return fc, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", type=Path, help="local cartographic boundary zip (default: download --url)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--statefp", default="06", help="state FIPS code (06 = California)")
    ap.add_argument("--out", type=Path, default=Path("config/regions/california.geojson"))
    ap.add_argument("--simplify-m", type=float, default=0.0)
    ap.add_argument("--max-error-m", type=float, default=500.0)
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="fuellake-region-") as tmp:
        zip_path = args.zip
        if zip_path is None:
            zip_path = Path(tmp) / args.url.rsplit("/", 1)[-1]
            _download(args.url, zip_path)
        md5 = hashlib.md5(zip_path.read_bytes(), usedforsecurity=False).hexdigest()
        fc, stats = build(zip_path, args.statefp, args.simplify_m, args.max_error_m)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({**stats, "zip_md5": md5, "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
