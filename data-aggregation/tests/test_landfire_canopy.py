# fuel-lake — tests for the LANDFIRE canopy connector (attribute CSVs, scales, non-forested handling, pipeline)
# Licensed under Apache-2.0.
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import yaml
from affine import Affine
from rasterio.io import MemoryFile

from conftest import TINY_AOI
from fuellake import catalog, pipeline
from fuellake.connectors.base import Period
from fuellake.connectors.landfire_canopy import (
    LandfireCanopyConnector,
    check_scale,
    codes_to_physical,
    parse_value_table,
)
from fuellake.contracts import validate_collection
from fuellake.rasters import read_cog
from fuellake.registry import load_dataset

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
FIX = Path(__file__).parent / "fixtures" / "landfire_canopy"
DS = "landfire-canopy"


def _csv(code: str) -> str:
    return (FIX / f"LF2025_{code}.csv").read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------- official attribute tables
def test_attribute_tables_and_scales():
    col, cc = parse_value_table(_csv("CC"))
    assert col == "CC_PERCENT" and sorted(cc) == [0, 15, 25, 35, 45, 55, 65, 75, 85, 95]
    assert cc[0] == "Non-Forested"
    col, ch = parse_value_table((FIX / "LF2025_CH.csv").read_text(encoding="utf-8"))   # BOM left in on purpose
    assert col == "METERSX10" and 0 in ch and 30 in ch and max(ch) <= 510
    col, cbh = parse_value_table(_csv("CBH"))
    assert col == "METERSX10" and sorted(cbh) == list(range(0, 101))
    col, cbd = parse_value_table(_csv("CBD"))
    assert col == "KGM3_X_100" and sorted(cbd) == list(range(0, 46))
    assert cbd[1].startswith("CBD = 01 kg/m^3 X 100")
    for name, code in (("canopy_cover", "CC"), ("canopy_height", "CH"), ("canopy_base_height", "CBH"),
                       ("canopy_bulk_density", "CBD")):
        scale = load_dataset(DS).options["layers"][name]["scale"]
        check_scale(name, parse_value_table(_csv(code))[0], scale)


def test_scale_mismatch_fails():
    with pytest.raises(ValueError, match="implies 0.1"):
        check_scale("canopy_height", "METERSX10", 1.0)
    with pytest.raises(ValueError, match="unknown LANDFIRE value column"):
        check_scale("canopy_height", "FEET", 1.0)


def test_codes_to_physical():
    _, ch = parse_value_table(_csv("CH"))
    codes = np.array([[0, 30, 510], [-9999, 31, 110]], dtype="int16")
    out, unknown = codes_to_physical(codes, ch, 0.1, 0.0)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[0], [0.0, 3.0, 51.0], rtol=1e-6)
    assert np.isnan(out[1, 0]) and np.isnan(out[1, 1]) and out[1, 2] == pytest.approx(11.0)
    assert unknown == pytest.approx(1 / 5)          # 31 is not a CH class
    _, cbh = parse_value_table(_csv("CBH"))
    out, _ = codes_to_physical(np.array([[0, 7, 100]], dtype="int16"), cbh, 0.1, float("nan"))
    assert np.isnan(out[0, 0]) and out[0, 1] == pytest.approx(0.7) and out[0, 2] == pytest.approx(10.0)


# ---------------------------------------------------------------------------- offline pipeline
def _service(code: str) -> dict:
    return {"name": f"Landfire_LF2025/LF2025_{code}_CONUS", "pixelType": "S16", "noDataValue": -9999,
            "pixelSizeX": 30, "pixelSizeY": 30, "capabilities": "Image,Metadata,Catalog,Mensuration",
            "extent": {"xmin": -2362395, "ymin": 221265, "xmax": 2327655, "ymax": 3267405,
                       "spatialReference": {"wkid": 5070, "latestWkid": 5070}},
            "spatialReference": {"wkid": 5070, "latestWkid": 5070}}


SYNTH = {"CC": [0, 15, 55, 95], "CH": [0, 30, 150, 510], "CBH": [0, 5, 23, 100], "CBD": [0, 4, 12, 45]}


class _Resp:
    def __init__(self, content: bytes, ctype: str):
        self.content, self.headers, self.status_code = content, {"content-type": ctype}, 200
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.content)


def _clip(code: str, params: dict) -> bytes:
    xmin, ymin, xmax, ymax = (float(v) for v in params["bbox"].split(","))
    w, h = (int(v) for v in params["size"].split(","))
    vals = np.array(SYNTH[code], dtype="int16")
    arr = vals[(np.arange(w)[None, :].repeat(h, 0) // 10) % len(vals)]
    arr[-5:, :] = -9999
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=w, height=h, count=1, dtype="int16", crs="EPSG:5070",
                      transform=Affine(30, 0, xmin, 0, -30, ymax), nodata=-9999) as ds:
            ds.write(arr, 1)
        return mem.read()


def _fake_get(calls):
    def get(self, url, **params):
        calls.append(url)
        code = next(c for c in ("CBH", "CBD", "CC", "CH") if f"_{c}_" in url or url.endswith(f"_{c}.csv"))
        if url.endswith("/exportImage"):
            return _Resp(_clip(code, params), "image/tiff")
        if url.endswith(".csv"):
            return _Resp((FIX / f"LF2025_{code}.csv").read_bytes(), "text/csv")
        if url.endswith(".xml"):
            return _Resp(f"<metadata><title>LANDFIRE 2025 {code}</title><edition>LF 2025</edition>"
                         f"<pubdate>20260430</pubdate></metadata>".encode(), "text/xml")
        return _Resp(json.dumps(_service(code)).encode(), "application/json")
    return get


@pytest.fixture
def canopy_config(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "config"
    (root / "aois").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "aois" / "tiny.yaml").write_text(yaml.safe_dump(TINY_AOI))
    shutil.copy(REPO_CONFIG / "datasets" / f"{DS}.yaml", root / "datasets" / f"{DS}.yaml")
    monkeypatch.setenv("FUELLAKE_CONFIG_DIR", str(root))
    return root


def test_pipeline_offline_canopy(lake, canopy_config, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(LandfireCanopyConnector, "_get", _fake_get(calls))
    rep = pipeline.run([DS], "tiny", Period(date(2026, 6, 1), date(2026, 6, 2)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    assert item["id"] == "landfire-canopy__tiny__20250101__lf2025"
    p = item["properties"]
    assert p["fuellake:resampling"] == "nearest" and p["fuellake:qc_masked_fraction"] == 0.0
    assert 0 < p["fuellake:forested_fraction"] < 1
    assert p["fuellake:landfire_layers"]["canopy_height"]["value_column"] == "METERSX10"
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))

    cc, _ = read_cog(lake, catalog.asset_rel(item, "canopy_cover"))
    ch, _ = read_cog(lake, catalog.asset_rel(item, "canopy_height"))
    cbh, _ = read_cog(lake, catalog.asset_rel(item, "canopy_base_height"))
    cbd, _ = read_cog(lake, catalog.asset_rel(item, "canopy_bulk_density"))
    assert set(np.unique(cc[np.isfinite(cc)]).tolist()) <= {0.0, 15.0, 55.0, 95.0}      # nearest keeps bins
    assert np.nanmax(ch) == pytest.approx(51.0) and np.nanmax(cbd) == pytest.approx(0.45)
    assert np.nanmax(cbh) == pytest.approx(10.0)
    assert np.isnan(cbh[cc == 0]).all()                                               # non-forested
    assert (ch[cc == 0] == 0).all() and (cbd[cc == 0] == 0).all()

    n = len(calls)
    rep2 = pipeline.run([DS], "tiny", Period(date(2026, 7, 1), date(2026, 7, 2)), lake=lake)
    assert (rep2.datasets[0].skipped, len(rep2.datasets[0].units)) == (1, 0)
    assert len(calls) == n + 4                       # only the four service checks in discover


# ---------------------------------------------------------------------------- live
@pytest.mark.network
def test_live_canopy_tiny_aoi(lake, canopy_config):
    rep = pipeline.run([DS], "tiny", Period(date(2026, 11, 1), date(2026, 11, 14)), lake=lake)
    d = rep.datasets[0]
    assert d.status == "ok", d.error or [u.error for u in d.units]
    item = next(catalog.iter_items(lake, DS))
    cc, _ = read_cog(lake, catalog.asset_rel(item, "canopy_cover"))
    ch, _ = read_cog(lake, catalog.asset_rel(item, "canopy_height"))
    assert np.isfinite(cc).any() and set(np.unique(cc[np.isfinite(cc)])) <= {0, 15, 25, 35, 45, 55, 65, 75, 85, 95}
    assert np.nanmax(ch) <= 51.0
    assert all(r.ok for r in validate_collection(lake, load_dataset(DS)))
