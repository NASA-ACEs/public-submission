# fuel-lake — surface fuel loads, fuel bed depth and extinction moisture derived from LANDFIRE FBFM40 items
# Licensed under Apache-2.0.
"""Scott and Burgan (2005) fuel model parameters mapped onto the ``landfire-fbfm40`` items of the same lake.

Source table: ``config/tables/fbfm40_parameters.csv``, transcribed verbatim from Table 7 of Scott, J.H. and
Burgan, R.E. (2005) *Standard fire behavior fuel models*, USDA Forest Service RMRS-GTR-153, printed page 18
(PDF page 25 of https://research.fs.usda.gov/download/treesearch/9521.pdf); the fuel model numbers (91-204) and
names come from the fuel model page headings (printed pages 20-70). The CSV header carries the full citation, the
PDF checksum and the unit notes. ``tests/test_landfire_fuel_load.py`` re-parses the PDF text of Table 7 and checks
every row against the fine fuel load and packing ratio printed on the fuel model pages.

Units and conversions
---------------------
- Loads are published in t/ac. The ton is the US short ton (2000 lb; evidence: the packing ratios printed on the
  fuel model pages are reproduced only with 2000 lb, see the CSV header), so
  ``1 t/ac = 2000 * 0.45359237 kg / 4046.8564224 m2 = 0.2241702 kg/m2`` (:data:`KG_M2_PER_TON_ACRE`).
- Depth ft -> m (x 0.3048). Moisture of extinction stays in percent (dead fuel, of dry weight).

Units of work
-------------
``discover`` returns one unit per cataloged ``landfire-fbfm40`` item of the AOI (the period is ignored: static
layers), i.e. **one derived item per FBFM40 release** (LF 2016, LF 2022, LF 2025, ...). The key is
``<fbfm40 item id>|<fbfm40 source key>|table-<sha256[:12] of the CSV>``, so the unit is redone when either the
source item or the parameter table changes. Each derived item takes its source item's datetime, start/end
datetime and release discriminator (``landfire-fuel-load__smm__20160101__lf2016`` from
``landfire-fbfm40__smm__20160101__lf2016``), so "latest release on or before the date" picks matching releases
in both collections. No FBFM40 item for the AOI is an error ("run landfire-fbfm40 first"). ``extract`` reads
nothing from the network (returns ``[]``).

Outputs (form A, exactly the AOI grid of the source item, no resampling)
------------------------------------------------------------------------
``total_load`` (sum of the five load classes), ``dead_1h_load``, ``dead_10h_load``, ``dead_100h_load``,
``live_herb_load``, ``live_woody_load`` (kg/m2, float32), ``fuel_bed_depth`` (m), ``moisture_of_extinction`` (%,
NaN where non-burnable: undefined), ``burnable`` (uint8: 1 burnable model, 0 NB1-NB9, 255 nodata). Non-burnable
codes get 0 load and 0 depth. Codes missing from the table become nodata and are counted in
``fuellake:qc_masked_fraction``. The parameter CSV is attached as asset ``parameters`` (role ``metadata``).

Lineage: ``fuellake:derived_from`` names the source collection, item id, source key, asset and LANDFIRE
release; ``fuellake:source_ids`` lists the source item id, its catalog path and the table path;
``fuellake:landfire_version`` / ``fuellake:landfire_release_year`` / ``fuellake:landfire_disturbances`` are
copied from the source item.

Loads are the static table values of each model. For dynamic models (GR, GS, SH1, SH9, TU1, TU3) the fire
behaviour model moves part of the live herbaceous load to dead 1-h as the herbs cure; that transfer depends on
live herbaceous moisture and is **not** applied here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from fuellake import catalog
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedAsset, StagedItem
from fuellake.rasters import mask_out_of_range, read_cog, write_cog
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.landfire_fuel_load")

LB_PER_SHORT_TON = 2000.0
KG_PER_LB = 0.45359237                       # exact (international pound)
M2_PER_ACRE = 4046.8564224                   # exact (international acre: 43560 ft2 x 0.3048^2)
KG_M2_PER_TON_ACRE = LB_PER_SHORT_TON * KG_PER_LB / M2_PER_ACRE   # 0.224170231...
M_PER_FT = 0.3048
FT2_PER_ACRE = 43560.0
PARTICLE_DENSITY_LB_FT3 = 32.0               # RMRS-GTR-153 printed p. 17, constant for the set
DEFAULT_TABLE = "tables/fbfm40_parameters.csv"
DEFAULT_SOURCE = "landfire-fbfm40"
CSV_MEDIA_TYPE = "text/csv"
FLAG_NODATA = 255
LOAD_CLASSES = ("load_1h_tac", "load_10h_tac", "load_100h_tac", "load_live_herb_tac", "load_live_woody_tac")
# raster name -> CSV load column
LOAD_LAYERS = {"dead_1h_load": "load_1h_tac", "dead_10h_load": "load_10h_tac", "dead_100h_load": "load_100h_tac",
               "live_herb_load": "load_live_herb_tac", "live_woody_load": "load_live_woody_tac"}
CITATION = ("Scott, J.H.; Burgan, R.E. 2005. Standard fire behavior fuel models: a comprehensive set for use with "
            "Rothermel's surface fire spread model. Gen. Tech. Rep. RMRS-GTR-153. USDA Forest Service, Rocky Mountain "
            "Research Station. Table 7, p. 18. doi:10.2737/RMRS-GTR-153")


# ---------------------------------------------------------------------------- table
@dataclass(frozen=True)
class FuelModel:
    """One row of the parameter table, in the publication's units (t/ac, 1/ft, ft, %, BTU/lb)."""

    code: int
    model: str
    name: str
    burnable: bool
    loads_tac: dict[str, float]          # LOAD_CLASSES -> t/ac
    fuel_model_type: str | None
    sav_per_ft: dict[str, float | None]  # 1h, live_herb, live_woody (9999 = no load in that class)
    depth_ft: float | None
    mx_dead_pct: float | None
    heat_btu_per_lb: float | None

    @property
    def total_load_tac(self) -> float:
        return sum(self.loads_tac.values())

    @property
    def fine_load_tac(self) -> float:
        """Dead 1-h + live herbaceous + live woody: the 'fine fuel load' printed on each fuel model page."""
        return self.loads_tac["load_1h_tac"] + self.loads_tac["load_live_herb_tac"] + self.loads_tac[
            "load_live_woody_tac"]

    def packing_ratio(self, lb_per_ton: float = LB_PER_SHORT_TON) -> float | None:
        """Total load / (depth x particle density), dimensionless, as printed on the fuel model pages."""
        if not self.depth_ft:
            return None
        return self.total_load_tac * lb_per_ton / FT2_PER_ACRE / self.depth_ft / PARTICLE_DENSITY_LB_FT3

    def si(self) -> dict[str, float]:
        """Raster values in SI units: loads kg/m2, depth m, moisture of extinction % (NaN when undefined)."""
        out = {name: self.loads_tac[col] * KG_M2_PER_TON_ACRE for name, col in LOAD_LAYERS.items()}
        out["total_load"] = self.total_load_tac * KG_M2_PER_TON_ACRE
        out["fuel_bed_depth"] = (self.depth_ft or 0.0) * M_PER_FT
        out["moisture_of_extinction"] = float(self.mx_dead_pct) if self.mx_dead_pct is not None else float("nan")
        return out


def _num(v: str | None) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def parse_parameter_table(text: str) -> dict[int, FuelModel]:
    """``{code: FuelModel}`` from the CSV text (lines starting with ``#`` are comments)."""
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    out: dict[int, FuelModel] = {}
    for row in csv.DictReader(io.StringIO(body)):
        code = int(row["code"])
        if code in out:
            raise ValueError(f"duplicate fuel model code {code} in the parameter table")
        burnable = row["burnable"].strip().lower() == "true"
        loads = {c: float(row[c]) for c in LOAD_CLASSES}
        if any(v < 0 for v in loads.values()):
            raise ValueError(f"negative load for code {code}")
        fm = FuelModel(
            code=code, model=row["model"].strip(), name=row["name"].strip(), burnable=burnable, loads_tac=loads,
            fuel_model_type=(row["fuel_model_type"].strip() or None),
            sav_per_ft={"1h": _num(row["sav_1h_per_ft"]), "live_herb": _num(row["sav_live_herb_per_ft"]),
                        "live_woody": _num(row["sav_live_woody_per_ft"])},
            depth_ft=_num(row["fuel_bed_depth_ft"]), mx_dead_pct=_num(row["mx_dead_pct"]),
            heat_btu_per_lb=_num(row["heat_content_btu_per_lb"]),
        )
        if burnable and (fm.depth_ft is None or fm.mx_dead_pct is None or fm.total_load_tac <= 0):
            raise ValueError(f"burnable code {code} needs loads, depth and moisture of extinction")
        if not burnable and fm.total_load_tac != 0:
            raise ValueError(f"non-burnable code {code} must have zero load")
        out[code] = fm
    if not out:
        raise ValueError("parameter table has no rows")
    return out


def lookup_arrays(table: dict[int, FuelModel], max_code: int = 255) -> dict[str, np.ndarray]:
    """Dense lookup vectors indexed by fuel model code: SI values (NaN for unknown codes) and ``burnable``."""
    names = [*LOAD_LAYERS, "total_load", "fuel_bed_depth", "moisture_of_extinction"]
    lut = {n: np.full(max_code + 1, np.nan, dtype="float64") for n in names}
    flag = np.full(max_code + 1, FLAG_NODATA, dtype="uint8")
    for code, fm in table.items():
        if not 0 <= code <= max_code:
            raise ValueError(f"fuel model code {code} outside 0..{max_code}")
        for n, v in fm.si().items():
            lut[n][code] = v
        flag[code] = 1 if fm.burnable else 0
    lut["burnable"] = flag
    return lut


def map_codes(codes: np.ndarray, table: dict[int, FuelModel], nodata: int) -> tuple[dict[str, np.ndarray], float]:
    """Map an FBFM40 code raster to the output layers.

    Returns ``(layers, unknown_fraction)``. ``nodata`` pixels and codes missing from the table give NaN
    (``burnable`` = 255); ``unknown_fraction`` is the share of data pixels whose code is not in the table.
    """
    codes = np.asarray(codes)
    lut = lookup_arrays(table)
    max_code = len(lut["burnable"]) - 1
    has_data = codes != nodata
    in_range = has_data & (codes >= 0) & (codes <= max_code)
    idx = np.where(in_range, codes, 0).astype("int64")
    known = in_range & (lut["burnable"][idx] != FLAG_NODATA)
    out: dict[str, np.ndarray] = {}
    for name, vec in lut.items():
        if name == "burnable":
            arr = np.where(known, vec[idx], FLAG_NODATA).astype("uint8")
        else:
            arr = np.where(known, vec[idx], np.nan).astype("float32")
        out[name] = arr
    n = int(has_data.sum())
    unknown = float((has_data & ~known).sum()) / n if n else 0.0
    return out, unknown


def table_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- connector
class LandfireFuelLoadConnector(BaseConnector):
    """Fuel loads etc. from the ``landfire-fbfm40`` items already cataloged in the same lake."""

    def _opts(self) -> dict:
        o = self.dataset.options
        return {"source_dataset": o.get("source_dataset", DEFAULT_SOURCE),
                "source_asset": o.get("source_asset", "fuel_model"),
                "table": o.get("parameter_table", DEFAULT_TABLE)}

    def parameter_table_path(self) -> Path:
        p = Path(self._opts()["table"])
        return p if p.is_absolute() else Path(self.settings.config_dir) / p

    def table_text(self) -> str:
        path = self.parameter_table_path()
        if not path.is_file():
            raise FileNotFoundError(f"parameter table {path} not found (option parameter_table)")
        return path.read_text(encoding="utf-8")

    def source_items(self, aoi: AOI) -> list[dict]:
        src = self._opts()["source_dataset"]
        return sorted((it for it in catalog.iter_items(self.lake, src)
                       if it["properties"].get("fuellake:aoi") == aoi.id), key=lambda it: it["id"])

    # ------------------------------------------------------------------ discover
    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        o = self._opts()
        text = self.table_text()
        parse_parameter_table(text)                       # fail early on a broken table
        sha = table_sha256(text)
        items = self.source_items(aoi)
        if not items:
            raise RuntimeError(f"no {o['source_dataset']} item for AOI {aoi.id} in this lake: run "
                               f"`fuellake run {o['source_dataset']} --aoi {aoi.id}` first")
        refs = []
        for it in items:
            p = it["properties"]
            dt = catalog.to_utc(p["datetime"])
            key = f"{it['id']}|{p.get('fuellake:source_key')}|table-{sha[:12]}"
            refs.append(self.ref(key, dt, urls=[catalog.item_rel(o["source_dataset"], it["id"])],
                                 source_item_id=it["id"], source_key=p.get("fuellake:source_key"),
                                 table_sha256=sha, start=p.get("start_datetime"), end=p.get("end_datetime")))
        return refs

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        return []   # everything is already in the lake (source item) or in config (parameter table)

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        o = self._opts()
        src_id = ref.meta["source_item_id"]
        item = catalog.get_item(self.lake, o["source_dataset"], src_id)
        if item is None:
            raise RuntimeError(f"source item {o['source_dataset']}/{src_id} disappeared")
        text = self.table_text()
        if table_sha256(text) != ref.meta["table_sha256"]:
            raise RuntimeError("parameter table changed between discover and transform; rerun")
        table = parse_parameter_table(text)
        self._check_labels(item, table)

        src_rel = catalog.asset_rel(item, o["source_asset"])
        codes, grid = read_cog(self.lake, src_rel)
        if not grid.same_as(aoi.grid.crs, aoi.grid.transform, aoi.grid.width, aoi.grid.height):
            raise ValueError(f"{src_id}: fuel_model grid differs from the AOI grid of {aoi.id}")
        band = (item["assets"][o["source_asset"]].get("raster:bands") or [{}])[0]
        nodata = int(band.get("nodata", -9999))
        layers, unknown = map_codes(codes, table, nodata)
        if not (layers["burnable"] != FLAG_NODATA).any():
            raise ValueError(f"{src_id}: no pixel with a known fuel model code")

        parts = src_id.split("__")
        iid = self.item_id(aoi, ref, discriminator=parts[3] if len(parts) > 3 else None)
        assets: list[StagedAsset] = []
        qc: dict[str, float] = {}
        stats: dict[str, dict] = {}
        for var in self.dataset.variables:
            arr = layers[var.name]
            path = self.cog_path(aoi, iid, var.name)
            if var.is_float:
                arr, frac = mask_out_of_range(arr, var.valid_range)
                qc[var.name] = frac
                finite = arr[np.isfinite(arr)]
                stats[var.name] = ({"min": float(finite.min()), "max": float(finite.max()),
                                    "mean": float(finite.mean())} if finite.size else {})
                write_cog(self.lake, path, arr, grid, np.nan, var.dtype, description=var.name, unit=var.unit)
            else:
                write_cog(self.lake, path, arr, grid, int(var.nodata), var.dtype, description=var.name,
                          tags={"VALUES": "1=burnable fuel model;0=non-burnable (NB1-NB9);255=nodata"})
            assets.append(self.cog_asset(var.name, path))
        table_rel = f"{self.staged_dir(aoi, iid)}/fbfm40_parameters.csv"
        self.lake.write_text(table_rel, text)
        assets.append(StagedAsset(key="parameters", path=table_rel, media_type=CSV_MEDIA_TYPE, roles=["metadata"]))

        flag = layers["burnable"]
        n_data = int((flag != FLAG_NODATA).sum())
        sp = item["properties"]
        props = {
            "fuellake:qc_masked_fraction": max([unknown, *qc.values()]),
            "fuellake:unknown_code_fraction": unknown,
            "fuellake:qc_masked_fraction_by_variable": qc,
            "fuellake:derived_from": {"collection": o["source_dataset"], "item_id": src_id,
                                      "source_key": ref.meta.get("source_key"), "asset": o["source_asset"],
                                      "landfire_version": sp.get("fuellake:landfire_version"),
                                      "datetime": sp.get("datetime"),
                                      "href": catalog.relative_href(catalog.item_rel(self.dataset.id, iid),
                                                                    catalog.item_rel(o["source_dataset"], src_id))},
            "fuellake:source_ids": [src_id, catalog.item_rel(o["source_dataset"], src_id), f"config/{o['table']}"],
            "fuellake:parameter_table": {"path": f"config/{o['table']}", "sha256": ref.meta["table_sha256"],
                                         "citation": CITATION,
                                         "pdf": "https://research.fs.usda.gov/download/treesearch/9521.pdf"},
            "fuellake:unit_conversion": {"load": f"1 t/ac (US short ton) = {KG_M2_PER_TON_ACRE:.7f} kg/m2",
                                         "depth": "1 ft = 0.3048 m"},
            "fuellake:burnable_fraction": round(float((flag == 1).sum()) / n_data, 6) if n_data else 0.0,
            "fuellake:value_stats": stats,
            "fuellake:landfire_version": sp.get("fuellake:landfire_version"),
            "fuellake:landfire_release_year": sp.get("fuellake:landfire_release_year"),
            "fuellake:landfire_disturbances": sp.get("fuellake:landfire_disturbances"),
            "fuellake:curing": "none applied: static Table 7 loads (dynamic models not cured)",
            "fuellake:stats_scope": "AOI grid before the pipeline's region clip (see fuellake:region_clip)",
        }
        start = catalog.to_utc(ref.meta["start"]) if ref.meta.get("start") else None
        end = catalog.to_utc(ref.meta["end"]) if ref.meta.get("end") else None
        return [StagedItem(item_id=iid, datetime=ref.datetime, properties=props, assets=assets,
                           start_datetime=start, end_datetime=end)]

    @staticmethod
    def _check_labels(item: dict, table: dict[int, FuelModel]) -> None:
        """The source item's code table (LANDFIRE CSV) must agree with the parameter table's model names."""
        classes = item["properties"].get("fuellake:classes") or {}
        bad = [f"{k}: LANDFIRE {v!r} vs table {table[int(k)].model!r}" for k, v in classes.items()
               if int(k) in table and str(v).strip().upper() != table[int(k)].model.upper()]
        if bad:
            raise ValueError("FBFM40 code labels disagree with the parameter table: " + "; ".join(bad))
