# fuel-lake — connectors: NASA SMAP soil moisture L4 / L3 enhanced (9 km EASE-Grid 2.0) and its climatology
# Licensed under Apache-2.0.
"""NASA SMAP soil moisture on the 9 km EASE-Grid 2.0, three form B datasets.

- ``smap-l4`` (:class:`SmapL4Connector`): SPL4SMGP, the L4 model + assimilation product, gap-free.
- ``smap-l3e`` (:class:`SmapL3eConnector`): SPL3SMP_E, the enhanced L3 passive retrieval, an observation
  with swath gaps.
- ``smap-climatology`` (:class:`SmapClimatologyConnector`): a day-of-year climatology (31-day window mean,
  standard deviation and count per cell) computed from the cataloged ``smap-l4`` and ``smap-l3e`` items.

SPL4SMGP (all facts checked live on 2026-11-14)
-----------------------------------------------
- CMR collection ``C3480440870-NSIDC_CPRD`` (NSIDC DAAC, Earthdata Cloud), short name ``SPL4SMGP``, version
  ``008``, DOI 10.5067/T5RUATAQREF8, 2015-03-31 to present, 8 granules per day (3-hour time averages centred
  at 01:30, 04:30, ..., 22:30 UTC). Latency: the newest granule (2026-11-10T22:30) was produced
  2026-11-12T22:56, i.e. about 2 days after its window and 3.5-4 days behind real time.
- Version 008 is published in sub-version streams (``VersionID`` in the file name): ``Vv8010`` (reprocessing,
  2015-03-31..2024-04-03), ``Vv8012`` (2023-08-01..2025-09-30), ``Vv8013`` (2025-10-01..) and 9 scattered
  ``Vv8011`` granules (2024-10-16..2026-02-07). A few time slots exist in two streams;
  :func:`select_granules` keeps the highest ``VersionID`` per slot. 2026 GranuleURs carry a suffix
  (``..._001.h5_u9loH5HD``).
- Each file is a global HDF5 file of about 143 MB. Datasets used (paths inside the file):
  ``/Geophysical_Data/sm_surface`` (0-5 cm), ``/Geophysical_Data/sm_rootzone`` (0-100 cm),
  ``/Geophysical_Data/sm_profile`` (0 cm to model bedrock): float32 ``m3 m-3``, ``_FillValue`` -9999
  (open water, ice), ``valid_min`` 0, ``valid_max`` 0.9; ``/x`` and ``/y`` (float64 cell centres, m);
  ``/time`` (seconds since J2000, see below); ``/EASE2_global_projection`` (lambert_cylindrical_equal_area,
  standard parallel 30). There is **no per-cell quality flag** in this product; the analysis uncertainty
  (ensemble spread) is only in the separate Analysis Update product SPL4SMAU, which is not staged.
- L4_SM is a model product: the NASA Catchment land model forced by GEOS-5 meteorology with observed
  precipitation, into which SMAP L-band brightness temperatures are assimilated. It is spatially and
  temporally complete (no orbit gaps), but it is not a direct retrieval.

SPL3SMP_E (checked live on 2026-11-14)
--------------------------------------
- CMR collection ``C2938664763-NSIDC_CPRD`` (NSIDC DAAC, Earthdata Cloud), short name ``SPL3SMP_E``, version
  ``006`` ("SMAP Enhanced L3 Radiometer Global and Polar Grid Daily 9 km EASE-Grid Soil Moisture V006"), DOI
  10.5067/M20OXIZHY3RJ, 2015-03-31 to present, one granule per UTC day (HDF5 of 570-660 MB), about one day
  latency (2026-11-12 produced 2026-11-13T19:27). GranuleUR
  ``SMAP_L3_SM_P_E_<yyyymmdd>_<CRID>_<counter>.h5[_suffix]``. For 2015-10-01..2026-11-12 CMR lists 3984
  granules on 3977 days, all CRID ``R19240``, counters 001-010 (a re-delivered day gets a higher counter; 7
  days of Oct-Nov 2026 are listed twice) and 84 days without a granule (2019-06-20..2019-07-22 and
  2022-08-07..2022-09-19, 33 and 44 days of instrument outage, plus 7 single days).
  :func:`select_l3e_granules` keeps one granule per day (highest CRID, then counter, then production time).
- Groups (paths inside the file): ``Soil_Moisture_Retrieval_Data_AM`` (6 a.m. local-time descending half
  orbits; over California the footprints are observed at about 14:00 UTC of the file's day),
  ``Soil_Moisture_Retrieval_Data_PM`` (6 p.m. ascending; about 02:00 UTC of the file's day, i.e. the
  **previous local evening**; variable names end in ``_pm``) and the two polar groups (EASE-Grid 2.0 North,
  2000 x 2000, not used). The global groups are 1624 x 3856 cells of the same 9 km grid as SPL4SMGP, with
  anonymous dimensions and no x/y variables.
- ``soil_moisture`` (identical to ``soil_moisture_dca``: the DCA retrieval is the V006 baseline): float32
  ``cm**3/cm**3``, ``_FillValue`` -9999, ``valid_min`` 0.02, ``valid_max`` 0.5 (a few not-recommended
  values exceed it, up to 0.61 on 2024-07-01). ``retrieval_qual_flag`` (uint16, fill 65534,
  ``flag_meanings`` "Retrieval_recommended Retrieval_attempted Retrieval_success FT_retrieval_success"):
  a **set** bit 0 means the retrieval is *not*
  of recommended quality (bit 1 not attempted, bit 2 not successful, bit 3 freeze/thaw retrieval not
  successful). ``surface_flag`` records the reasons (static/radar water, coastal proximity, urban area,
  precipitation, snow or ice, frozen ground, mountainous terrain, dense vegetation, nadir region).
- ``EASE_row_index`` / ``EASE_column_index`` (uint16, fill 65534) give the global cell of every filled cell;
  :func:`check_l3e_subset` requires them to equal the analytic window (one cell = 9 km), which replaces the
  x/y check of SPL4SMGP.
- Quality policy per variable (config): ``quality_flag`` (HDF5 path) and ``reject_bits`` (values whose flag
  has any of these bits set become NaN). ``smap-l3e`` stages the AM retrieval twice, recommended quality
  only (``reject_bits: 1``) and every successful retrieval, plus the PM retrieval of recommended quality.

Access path: OPeNDAP in Earthdata Cloud
---------------------------------------
Every granule record of both products carries an ``OPENDAP DATA`` link
``https://opendap.earthdata.nasa.gov/collections/<concept id>/granules/<GranuleUR>``. A DAP4 request with a
hyperslab constraint returns only the AOI window as a small netCDF-4 file, with the Earthdata Login bearer
token, in about 2 s (SPL4SMGP, three variables: 42 KB per day for ``smm``, 5 x 8 cells; about 110 KB per day
for ``ca``, 111 x 113 cells; SPL3SMP_E, six variables: about 61 KB per day for ``ca``)::

    <opendap>.dap.nc4?dap4.ce=/x[c0:c1];/y[r0:r1];/time;/Geophysical_Data/sm_surface[r0:r1][c0:c1];...
    <opendap>.dap.nc4?dap4.ce=/Soil_Moisture_Retrieval_Data_AM/soil_moisture[r0:r1][c0:c1];...

(DAP index ranges are inclusive.) Alternatives checked on 2026-11-14: NASA Harmony (``sds/HOSS``, itself
built on this OPeNDAP service) works with the token but takes about 18 s per job; AppEEARS lists
``SPL4SMGP.008`` but rejects the Earthdata bearer token (HTTP 403; it needs a username/password login);
HTTP range reads of the HDF5 file are no help because every variable is one gzip chunk of 1624 x 3856 cells
(about 19 MB transferred per granule for two variables).

Sampling
--------
SPL4SMGP: one granule per day (``options.sample_times_utc``, default ``["13:30"]``: the 12-15 UTC average,
about 04-07 h Pacific Standard Time, which contains the 6 a.m. descending SMAP overpass whose brightness
temperatures are assimilated, and the time of day when the soil profile is closest to equilibrium). Root
zone moisture changes little within a day; the surface layer has a diurnal cycle, so a fixed time of day
keeps the series consistent. Daily (not weekly) sampling is needed because the matchup builder reads form B
lags on the exact day ``sample date - L``. Several times per day can be configured; the cube and the
matchups then use their daily mean.

SPL3SMP_E: one granule per UTC day. ``time`` is 00:00 UTC of the granule's day (the day of the daily
composite); AM and PM retrievals are separate variables on that one time step, so the features layer (which
averages the time steps of a UTC day) never mixes them.

Units of work and output (form B)
---------------------------------
One unit per AOI and calendar month that has granules. A complete month (its last day is not after the
newest available day of the sampled slot) has key ``<dataset>-YYYYMM-<hash>``; the month still filling up
has key ``<dataset>-YYYYMM-through-YYYYMMDD-<hash>``. ``<hash>`` is the first 8 hex digits of the SHA-1 of
the selected GranuleURs, so a reprocessed month (new ``VersionID``/counter) or a newly published day gives a
new key and the item (whose id and path depend only on AOI and month) is replaced.

Raw subsets: ``raw/<dataset>/<yyyy>/<mm>/<granule stem>__<aoi>.nc4`` (the OPeNDAP responses as received).
They are reused when they hold the requested variables on the expected window, so an interrupted month
resumes where it stopped (and a ``--force`` rerun re-stages without downloading).

Staged: ``staged/<dataset>/<aoi>/<dataset>__<aoi>__YYYYMM01/data.zarr`` with dims ``(time, y, x)``, ``x``/``y``
the analytic EASE-Grid 2.0 cell centres in EPSG:6933 metres, ``spatial_ref`` for EPSG:6933, one float32
variable per config variable (NaN = fill, rejected quality or outside ``valid_range``). The window holds
every 9 km cell that intersects the AOI bbox (or its snapped target grid) plus ``options.buffer_cells``
(default 1) cells on every side.

Feature aliases (``alias_of``)
------------------------------
The features layer names a form B feature after its Zarr variable. A config variable with ``alias_of:
<other variable>`` (and no ``source_name``) is written as an exact copy of that variable, so a team contract
name such as ``smap_sm`` exists as a real variable (with its own ``lag`` flag). Only one dataset may declare
a given alias. Moving the alias to another dataset needs a ``--force`` rerun of both (raw subsets are
reused, nothing is downloaded); a features layer that read an alias field itself would make the copy
unnecessary.

Georeferencing: EASE-Grid 2.0 Global, EPSG:6933. The 9 km grid is 3856 x 1624 cells with the NSIDC
upper-left corner (-17367530.445, 7314540.830) m shared with the SME2 200 m grid
(:mod:`fuellake.connectors.nisar_sme2`), so the cell size is ``2 * 17367530.445 / 3856`` = 9008.0552 m. The
SPL4SMGP file's own ``x``/``y`` are float32-rounded (up to 0.9 m off at the grid edges); the connector writes
the analytic centres and refuses a subset whose file coordinates differ from them by more than
``options.georef_tolerance_m`` (default 5 m), which catches any window or grid mismatch (one cell = 9 km).

Time (SPL4SMGP): ``/time`` counts seconds since 2000-01-01T12:00:00 (J2000; the file's ``units`` attribute
states the UTC equivalent 11:58:55.816, so a CF decoder lands 64 s early). The connector uses the file name
time and checks ``/time`` against it within two minutes. ``time`` is the nominal centre of the 3-hour window.

Climatology (``smap-climatology``)
----------------------------------
Built from staged items only (no download): for each input of ``options.inputs`` (dataset, variable,
output prefix) every cataloged month of ``options.baseline`` must be present and complete, and all input
items must share one ``smap:window``; otherwise discovery returns nothing (and logs why). The unit key hashes
the input item ids and their source keys, so a reprocessed input month rebuilds the climatology.

Method (:func:`doy_climatology`): every daily value (the mean of a day's time steps) of the baseline is put on
the day-of-year step of a **leap reference year** (a date's month and day; 29 February is step 60 and occurs
only in 2016, 2020 and 2024; 1 March is step 61 in every year). For each of the 366 steps the climatology
pools all daily values whose circular day distance is at most ``window_days // 2`` (31-day window: 15 days
either side, wrapping around the year end, so 1 January uses 17 December..16 January of the adjacent
years). ``<prefix>_mean`` is their mean, ``<prefix>_std`` their sample standard deviation (ddof 1: it
contains interannual and within-window variability, not only the spread of yearly means) and
``<prefix>_count`` the number of values (gaps of an observation product lower it). Mean and std are NaN
where the count is below ``options.min_count``. With the baseline 2015-10-01..2024-12-31, October-December
pool ten years and January-September nine. The store's ``time`` axis is the 366 days of
``options.reference_year`` (default 1904, a leap year that no build period reaches: every ``raster-b``
dataset is a candidate weather input of the features layer, searched by period, and the core treats only
form A layers as static), with a ``dayofyear`` coordinate; use :func:`climatology_step` to find the step
of a date. The item's ``datetime`` / ``start_datetime`` / ``end_datetime`` span that reference year, so
search it by collection without a time filter.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time as _time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import numpy as np

from fuellake.config import redact
from fuellake.connectors import _earthdata as ed
from fuellake.connectors.base import BaseConnector, Period, SourceRef, StagedItem
from fuellake.connectors.gridmet import month_end, month_starts, utc_midnight
from fuellake.connectors.nisar_sme2 import EASE2_UL_X, EASE2_UL_Y, ease2_centre
from fuellake.rasters import mask_out_of_range, write_zarr
from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.smap")

DEFAULT_CONCEPT_ID = "C3480440870-NSIDC_CPRD"   # SPL4SMGP version 008
L3E_CONCEPT_ID = "C2938664763-NSIDC_CPRD"       # SPL3SMP_E version 006
EASE2_EPSG = 6933
EASE2_9KM_COLS = 3856
EASE2_9KM_ROWS = 1624
EASE2_9KM_CELL = 2.0 * abs(EASE2_UL_X) / EASE2_9KM_COLS     # 9008.055210145943 m
TIME_EPOCH = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)     # what /time actually counts from (see docstring)
TIME_TOLERANCE_S = 120.0
FILL = -9999.0
L3E_INDEX_FILL = 65534                                     # _FillValue of the uint16 flags and EASE indices
TOKEN_ENV = ed.TOKEN_ENV
# Hosts that receive the Earthdata bearer token: the shared allowlist of fuellake.connectors._earthdata
# (opendap.earthdata.nasa.gov and *.earthdatacloud.nasa.gov). CMR never does (it is searched without credentials).
TOKEN_HOSTS = ed.DATA_HOSTS
wants_token = ed.wants_token
CITATION = ("Reichle, R., De Lannoy, G., Koster, R. D., Crow, W. T., Kimball, J. S., Liu, Q. & Bechtold, M. "
            "(2025). SMAP L4 Global 3-hourly 9 km EASE-Grid Surface and Root Zone Soil Moisture Geophysical "
            "Data. (SPL4SMGP, Version 8). Boulder, Colorado USA. NASA National Snow and Ice Data Center "
            "Distributed Active Archive Center. https://doi.org/10.5067/T5RUATAQREF8")
L3E_CITATION = ("O'Neill, P. E., Chan, S., Njoku, E. G., Jackson, T., Bindlish, R., Chaubell, J. & Colliander, "
                "A. (2023). SMAP Enhanced L3 Radiometer Global and Polar Grid Daily 9 km EASE-Grid Soil "
                "Moisture. (SPL3SMP_E, Version 6). Boulder, Colorado USA. NASA National Snow and Ice Data Center "
                "Distributed Active Archive Center. https://doi.org/10.5067/M20OXIZHY3RJ")
LICENSE_TEXT = "NASA EOSDIS open data (CC0-1.0 per the ESDIS data use guidance); cite the dataset"
L3E_INDEX_PATHS = (("Soil_Moisture_Retrieval_Data_AM/EASE_row_index",
                    "Soil_Moisture_Retrieval_Data_AM/EASE_column_index"),)

GPH_NAME_RE = re.compile(
    r"^(?P<stem>SMAP_L4_SM_gph_(?P<time>\d{8}T\d{6})_(?P<version>Vv\d{4})_(?P<counter>\d{3}))"
    r"\.h5(?P<suffix>_[A-Za-z0-9]+)?$"
)
L3E_NAME_RE = re.compile(
    r"^(?P<stem>SMAP_L3_SM_P_E_(?P<date>\d{8})_(?P<crid>R\d{5})_(?P<counter>\d{3}))"
    r"\.h5(?P<suffix>_[A-Za-z0-9]+)?$"
)


class SmapAuthError(ed.EarthdataAuthError):
    """Missing or rejected Earthdata token for the OPeNDAP service (message never contains the token)."""


# ---------------------------------------------------------------------------- granule names and selection
@dataclass(frozen=True)
class SmapGranule:
    """One SMAP granule as listed by CMR (SPL4SMGP or SPL3SMP_E)."""

    ur: str              # GranuleUR (may carry a suffix after ".h5")
    stem: str            # file name without ".h5"
    time: str            # SPL4SMGP: nominal centre; SPL3SMP_E: 00:00 of the UTC day (ISO 8601 UTC)
    version: str         # SPL4SMGP VersionID ("Vv8013"); SPL3SMP_E CRID ("R19240")
    counter: int
    opendap: str         # OPeNDAP granule URL
    https: str | None = None
    production: str | None = None

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.time)

    @property
    def day(self) -> date:
        return self.dt.date()


GphGranule = SmapGranule      # the SPL4SMGP name used before SPL3SMP_E was added


def parse_gph_name(name: str) -> dict[str, Any] | None:
    """Fields of an SPL4SMGP file name or GranuleUR; ``None`` if it is not one."""
    m = GPH_NAME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    d: dict[str, Any] = m.groupdict()
    d["dt"] = datetime.strptime(d["time"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    d["counter"] = int(d["counter"])
    return d


def parse_l3e_name(name: str) -> dict[str, Any] | None:
    """Fields of an SPL3SMP_E file name or GranuleUR (``dt`` = 00:00 UTC of its day); ``None`` otherwise."""
    m = L3E_NAME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    d: dict[str, Any] = m.groupdict()
    d["dt"] = datetime.strptime(d["date"], "%Y%m%d").replace(tzinfo=UTC)
    d["counter"] = int(d["counter"])
    return d


def _related_urls(umm: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(OPeNDAP URL, https .h5 URL)`` of a UMM-G record. The raw record is used because
    :func:`fuellake.connectors._earthdata.parse_granule` keeps only ``GET DATA`` links and drops the
    ``USE SERVICE API`` / ``OPENDAP DATA`` one."""
    urls = umm.get("RelatedUrls", []) or []
    opendap = next((u["URL"] for u in urls if u.get("Subtype") == "OPENDAP DATA"
                    and str(u.get("URL", "")).startswith("https://")), None)
    https = next((u["URL"] for u in urls if u.get("Type") == "GET DATA"
                  and str(u.get("URL", "")).startswith("https://") and u["URL"].endswith(".h5")), None)
    return opendap, https


def parse_gph_item(item: Mapping[str, Any]) -> SmapGranule | None:
    """A :class:`SmapGranule` from one raw SPL4SMGP CMR UMM-G record (``None`` without a usable name or
    OPeNDAP link)."""
    umm = item.get("umm", {})
    ur = str(umm.get("GranuleUR", ""))
    name = parse_gph_name(ur)
    if name is None:
        return None
    opendap, https = _related_urls(umm)
    if opendap is None:
        return None
    prod = (umm.get("DataGranule") or {}).get("ProductionDateTime")
    return SmapGranule(ur=ur, stem=name["stem"], time=name["dt"].isoformat(), version=name["version"],
                       counter=name["counter"], opendap=opendap, https=https, production=prod)


def parse_l3e_item(item: Mapping[str, Any]) -> SmapGranule | None:
    """A :class:`SmapGranule` from one raw SPL3SMP_E CMR UMM-G record (``version`` = CRID)."""
    umm = item.get("umm", {})
    ur = str(umm.get("GranuleUR", ""))
    name = parse_l3e_name(ur)
    if name is None:
        return None
    opendap, https = _related_urls(umm)
    if opendap is None:
        return None
    prod = (umm.get("DataGranule") or {}).get("ProductionDateTime")
    return SmapGranule(ur=ur, stem=name["stem"], time=name["dt"].isoformat(), version=name["crid"],
                       counter=name["counter"], opendap=opendap, https=https, production=prod)


def sample_time_tokens(sample_times: Iterable[str]) -> list[str]:
    """``["13:30"]`` -> ``["133000"]`` (the ``HHMMSS`` of the file name); validates the 3-hour slots."""
    out = []
    for t in sample_times:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
        if not m:
            raise ValueError(f"sample time {t!r} must look like 'HH:MM' (UTC)")
        hh, mm = int(m.group(1)), int(m.group(2))
        if mm != 30 or hh % 3 != 1:
            raise ValueError(f"sample time {t!r} is not an SPL4SMGP slot (01:30, 04:30, ..., 22:30 UTC)")
        out.append(f"{hh:02d}{mm:02d}00")
    if not out:
        raise ValueError("options.sample_times_utc is empty")
    return sorted(set(out))


def _best_per_time(granules: Iterable[SmapGranule | None], first: date | None, last: date | None,
                   keep=lambda g: True) -> list[SmapGranule]:
    best: dict[str, SmapGranule] = {}
    for g in granules:
        if g is None or not keep(g):
            continue
        if (first and g.day < first) or (last and g.day > last):
            continue
        cur = best.get(g.time)
        if cur is None or (g.version, g.counter, g.production or "") > (cur.version, cur.counter,
                                                                         cur.production or ""):
            best[g.time] = g
    return [best[k] for k in sorted(best)]


def select_granules(items: Iterable[Mapping[str, Any]], sample_times: Iterable[str],
                    first: date | None = None, last: date | None = None) -> list[SmapGranule]:
    """SPL4SMGP granules of the sampled slots, one per nominal time (highest ``VersionID``, then counter,
    then production time), optionally restricted to ``first <= day <= last``; sorted by time."""
    tokens = set(sample_time_tokens(sample_times))
    return _best_per_time((parse_gph_item(it) for it in items), first, last,
                          keep=lambda g: g.dt.strftime("%H%M%S") in tokens)


def select_l3e_granules(items: Iterable[Mapping[str, Any]], first: date | None = None,
                        last: date | None = None) -> list[SmapGranule]:
    """SPL3SMP_E granules, one per UTC day (highest CRID, then counter, then production time), optionally
    restricted to ``first <= day <= last``; sorted by day."""
    return _best_per_time((parse_l3e_item(it) for it in items), first, last)


def granule_digest(granules: Sequence[SmapGranule]) -> str:
    return hashlib.sha1("\n".join(g.ur for g in granules).encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------- EASE-Grid 2.0 window maths
def ease2_window(bounds: Sequence[float], buffer_cells: int = 1, cell: float = EASE2_9KM_CELL,
                 ncols: int = EASE2_9KM_COLS, nrows: int = EASE2_9KM_ROWS) -> tuple[int, int, int, int]:
    """Global ``(row0, row1, col0, col1)`` (stop exclusive) of the cells whose extent intersects ``bounds``
    (EPSG:6933 metres: minx, miny, maxx, maxy) plus ``buffer_cells`` cells on every side, clipped to the grid.
    A bound lying on a cell edge (within ``1e-6`` cell, i.e. 9 mm) does not pull in the neighbouring cell."""
    minx, miny, maxx, maxy = (float(v) for v in bounds)
    if maxx <= minx or maxy <= miny:
        raise ValueError(f"empty bounds {tuple(bounds)}")
    eps = 1e-6
    c_first = int(np.floor((minx - EASE2_UL_X) / cell + eps))
    c_last = int(np.ceil((maxx - EASE2_UL_X) / cell - eps)) - 1
    r_first = int(np.floor((EASE2_UL_Y - maxy) / cell + eps))
    r_last = int(np.ceil((EASE2_UL_Y - miny) / cell - eps)) - 1
    b = int(buffer_cells)
    r0, r1 = max(0, r_first - b), min(nrows, r_last + b + 1)
    c0, c1 = max(0, c_first - b), min(ncols, c_last + b + 1)
    if r1 <= r0 or c1 <= c0:
        raise ValueError(f"bounds {tuple(bounds)} are outside the EASE-Grid 2.0 9 km grid")
    return r0, r1, c0, c1


def aoi_bounds_6933(aoi: AOI) -> tuple[float, float, float, float]:
    """EPSG:6933 bounds covering both the AOI WGS84 bbox and its (outward-snapped) target grid."""
    from rasterio.warp import transform_bounds

    a = transform_bounds("EPSG:4326", f"EPSG:{EASE2_EPSG}", *aoi.bbox, densify_pts=21)
    g = aoi.grid
    b = transform_bounds(g.crs, f"EPSG:{EASE2_EPSG}", *g.bounds(), densify_pts=21)
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def aoi_window(aoi: AOI, buffer_cells: int = 1) -> tuple[int, int, int, int]:
    return ease2_window(aoi_bounds_6933(aoi), buffer_cells)


def window_centres(window: Sequence[int], cell: float = EASE2_9KM_CELL) -> tuple[np.ndarray, np.ndarray]:
    """Analytic EPSG:6933 cell centres ``(x, y)`` of a ``(row0, row1, col0, col1)`` window (y decreasing)."""
    r0, r1, c0, c1 = window
    x, _ = ease2_centre(np.arange(c0, c1), 0, cell)
    _, y = ease2_centre(0, np.arange(r0, r1), cell)
    return np.asarray(x, dtype="float64"), np.asarray(y, dtype="float64")


def window_bounds_wgs84(window: Sequence[int], cell: float = EASE2_9KM_CELL) -> tuple[float, float, float, float]:
    """WGS84 bbox of the cell edges of a window."""
    from rasterio.warp import transform_bounds

    r0, r1, c0, c1 = window
    minx, maxy = EASE2_UL_X + c0 * cell, EASE2_UL_Y - r0 * cell
    maxx, miny = EASE2_UL_X + c1 * cell, EASE2_UL_Y - r1 * cell
    w, s, e, n = transform_bounds(f"EPSG:{EASE2_EPSG}", "EPSG:4326", minx, miny, maxx, maxy, densify_pts=21)
    return float(w), float(s), float(e), float(n)


# ---------------------------------------------------------------------------- OPeNDAP requests and parsing
def dap4_constraint(sources: Sequence[str], window: Sequence[int]) -> str:
    """SPL4SMGP DAP4 constraint expression for the window: x, y, time and the 2-D sources (DAP ranges are
    inclusive)."""
    r0, r1, c0, c1 = window
    parts = [f"/x[{c0}:{c1 - 1}]", f"/y[{r0}:{r1 - 1}]", "/time"]
    parts += [f"/{s.lstrip('/')}[{r0}:{r1 - 1}][{c0}:{c1 - 1}]" for s in sources]
    return ";".join(parts)


def dap4_constraint_2d(paths: Sequence[str], window: Sequence[int]) -> str:
    """DAP4 constraint of 2-D ``(row, col)`` variables only (SPL3SMP_E has no x/y/time variables)."""
    r0, r1, c0, c1 = window
    return ";".join(f"/{p.lstrip('/')}[{r0}:{r1 - 1}][{c0}:{c1 - 1}]" for p in paths)


def dap4_url(opendap: str, sources: Sequence[str], window: Sequence[int]) -> str:
    return f"{opendap}.dap.nc4?dap4.ce=" + quote(dap4_constraint(sources, window), safe="/")


def dap4_url_2d(opendap: str, paths: Sequence[str], window: Sequence[int]) -> str:
    return f"{opendap}.dap.nc4?dap4.ce=" + quote(dap4_constraint_2d(paths, window), safe="/")


def _read_h5_layers(f, sources: Sequence[str], out: dict[str, Any]) -> None:
    for s in sources:
        key = s.lstrip("/")
        if key not in f:
            raise KeyError(f"OPeNDAP subset has no /{key}")
        ds = f[key]
        out["arrays"][key] = np.asarray(ds[()])
        attrs: dict[str, Any] = {}
        for k in ("_FillValue", "valid_min", "valid_max"):
            if k in ds.attrs:
                attrs[k] = float(np.asarray(ds.attrs[k]).ravel()[0])
        if "units" in ds.attrs:
            attrs["units"] = ed.h5_str(ds.attrs["units"])
        out["attrs"][key] = attrs


def read_dap_subset(src: Any, sources: Sequence[str]) -> dict[str, Any]:
    """Read an SPL4SMGP OPeNDAP netCDF-4 subset (path or file-like): ``x``, ``y``, ``time`` (seconds since
    J2000), and per source the raw array and its ``_FillValue`` / ``valid_min`` / ``valid_max`` / ``units``."""
    import h5py

    out: dict[str, Any] = {"arrays": {}, "attrs": {}}
    with h5py.File(src, "r") as f:
        for name in ("x", "y", "time"):
            if name not in f:
                raise KeyError(f"OPeNDAP subset has no /{name}")
        out["x"] = np.asarray(f["x"][()], dtype="float64")
        out["y"] = np.asarray(f["y"][()], dtype="float64")
        out["time"] = float(np.asarray(f["time"][()]).ravel()[0])
        _read_h5_layers(f, sources, out)
    return out


def read_l3e_subset(src: Any, paths: Sequence[str]) -> dict[str, Any]:
    """Read an SPL3SMP_E OPeNDAP netCDF-4 subset: per path the raw array and its attributes."""
    import h5py

    out: dict[str, Any] = {"arrays": {}, "attrs": {}}
    with h5py.File(src, "r") as f:
        _read_h5_layers(f, paths, out)
    return out


def check_subset(sub: Mapping[str, Any], window: Sequence[int], sources: Sequence[str],
                 tol_m: float = 5.0, cell: float = EASE2_9KM_CELL) -> None:
    """Raise ``ValueError`` unless the SPL4SMGP subset holds every source on exactly ``window`` (file centres
    within ``tol_m`` of the analytic EASE-Grid 2.0 centres)."""
    r0, r1, c0, c1 = window
    ny, nx = r1 - r0, c1 - c0
    if sub["x"].shape != (nx,) or sub["y"].shape != (ny,):
        raise ValueError(f"subset x/y shapes {sub['x'].shape}/{sub['y'].shape} != window ({ny}, {nx})")
    _check_shapes(sub, window, sources)
    xe, ye = window_centres(window, cell)
    dx = float(np.max(np.abs(sub["x"] - xe)))
    dy = float(np.max(np.abs(sub["y"] - ye)))
    if dx > tol_m or dy > tol_m:
        raise ValueError(f"EASE-Grid 2.0 georeferencing mismatch: file centres differ from the analytic 9 km "
                         f"centres of window {tuple(window)} by {dx:.2f} m (x) / {dy:.2f} m (y) > {tol_m} m")


def _check_shapes(sub: Mapping[str, Any], window: Sequence[int], paths: Sequence[str]) -> None:
    r0, r1, c0, c1 = window
    ny, nx = r1 - r0, c1 - c0
    for s in paths:
        arr = sub["arrays"].get(s.lstrip("/"))
        if arr is None or arr.shape != (ny, nx):
            raise ValueError(f"subset {s}: shape {None if arr is None else arr.shape} != ({ny}, {nx})")


def check_l3e_subset(sub: Mapping[str, Any], window: Sequence[int], paths: Sequence[str],
                     index_paths: Sequence[Sequence[str]] = L3E_INDEX_PATHS) -> int:
    """Raise ``ValueError`` unless the SPL3SMP_E subset holds every path on ``window`` and, for each
    ``(row index, column index)`` pair of ``index_paths``, every filled cell carries its own global row and
    column. Returns the number of cells whose indices were checked (0: no retrieval in the window that day)."""
    _check_shapes(sub, window, paths)
    r0, r1, c0, c1 = window
    rows_e = np.arange(r0, r1)[:, None]
    cols_e = np.arange(c0, c1)[None, :]
    checked = 0
    for rp, cp in index_paths:
        rows = sub["arrays"].get(rp.lstrip("/"))
        cols = sub["arrays"].get(cp.lstrip("/"))
        if rows is None or cols is None:
            raise ValueError(f"subset lacks the EASE index variables {rp} / {cp}")
        fill = int(sub["attrs"].get(rp.lstrip("/"), {}).get("_FillValue", L3E_INDEX_FILL))
        have = (rows != fill) & (cols != fill)
        bad = have & ((rows != rows_e) | (cols != cols_e))
        if bad.any():
            i, j = (int(v[0]) for v in np.nonzero(bad))
            raise ValueError(f"EASE-Grid 2.0 georeferencing mismatch: cell ({i}, {j}) of window {tuple(window)} "
                             f"carries global index ({int(rows[i, j])}, {int(cols[i, j])}), expected "
                             f"({r0 + i}, {c0 + j})")
        checked += int(have.sum())
    return checked


def subset_time(seconds: float) -> datetime:
    """UTC time of an SPL4SMGP ``/time`` value (seconds since 2000-01-01T12:00:00, see the module docstring)."""
    return TIME_EPOCH + timedelta(seconds=float(seconds))


def layer_values(sub: Mapping[str, Any], source: str) -> np.ndarray:
    """float32 array of ``source`` with ``_FillValue`` (default -9999) and non-finite values as NaN."""
    key = source.lstrip("/")
    out = ed.fill_to_nan(sub["arrays"][key], sub["attrs"].get(key, {}).get("_FillValue", FILL))
    out[~np.isfinite(out)] = np.nan
    return out


def quality_masked(sub: Mapping[str, Any], source: str, flag: str | None = None,
                   reject_bits: int = 0) -> tuple[np.ndarray, int]:
    """:func:`layer_values` with the SPL3SMP_E quality policy: cells whose ``flag`` is fill or has any of
    ``reject_bits`` set become NaN. Returns ``(values, number of valid values rejected)``."""
    vals = layer_values(sub, source)
    if not flag:
        return vals, 0
    key = flag.lstrip("/")
    f = np.asarray(sub["arrays"][key]).astype("int64")
    fill = int(sub["attrs"].get(key, {}).get("_FillValue", L3E_INDEX_FILL))
    bad = (f == fill) | ((f & int(reject_bits)) != 0)
    rejected = np.isfinite(vals) & bad
    vals[rejected] = np.nan
    return vals, int(rejected.sum())


# ---------------------------------------------------------------------------- shared helpers
def _extra(v: Any, key: str) -> Any:
    return getattr(v, key, None) if key in type(v).model_fields else (v.model_extra or {}).get(key)


def alias_target(v: Any) -> str | None:
    """Name of the variable a config variable copies (``alias_of``), or ``None``."""
    t = _extra(v, "alias_of")
    return str(t) if t else None


def _days_through(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _xy_dataset(data_vars: dict, times: np.ndarray, window: Sequence[int]):
    """xarray Dataset on the window's analytic EASE-Grid 2.0 centres with ``spatial_ref`` (EPSG:6933)."""
    import rioxarray  # noqa: F401  (registers the .rio accessor)
    import xarray as xr

    x, y = window_centres(window)
    ds = xr.Dataset(data_vars, coords={"time": times, "y": y, "x": x})
    ds["x"].attrs.update({"units": "m", "standard_name": "projection_x_coordinate",
                          "long_name": "EASE-Grid 2.0 cell centre x"})
    ds["y"].attrs.update({"units": "m", "standard_name": "projection_y_coordinate",
                          "long_name": "EASE-Grid 2.0 cell centre y"})
    ds = ds.rio.set_spatial_dims(x_dim="x", y_dim="y").rio.write_crs(f"EPSG:{EASE2_EPSG}")
    for name in ds.data_vars:
        ds[name].encoding = {}
    return ds


# ---------------------------------------------------------------------------- daily OPeNDAP connectors
class _SmapDailyConnector(BaseConnector):
    """Shared machinery of the SMAP connectors that subset daily 9 km EASE-Grid 2.0 granules with OPeNDAP:
    month units, raw-subset cache, parallel fetches with retries, and alias variables."""

    default_concept_id = ""
    file_pattern = ""        # CMR GranuleUR pattern(s) restricting discovery

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._local = threading.local()

    # ------------------------------------------------------------------ config
    @property
    def concept_id(self) -> str:
        return str(self.dataset.options.get("concept_id", self.default_concept_id))

    @property
    def buffer_cells(self) -> int:
        return int(self.dataset.options.get("buffer_cells", 1))

    @property
    def max_workers(self) -> int:
        return max(1, int(self.dataset.options.get("max_workers", 4)))

    @property
    def retries(self) -> int:
        return max(0, int(self.dataset.options.get("retries", 3)))

    @property
    def request_timeout(self) -> float:
        return float(self.dataset.options.get("request_timeout_s", 120))

    @property
    def georef_tolerance_m(self) -> float:
        return float(self.dataset.options.get("georef_tolerance_m", 5.0))

    def source_variables(self) -> list:
        """Config variables read from the file (every variable that is not an ``alias_of`` copy)."""
        if not self.dataset.variables:
            raise ValueError(f"{self.dataset.id}: no variables configured")
        out = [v for v in self.dataset.variables if not alias_target(v)]
        names = {v.name for v in out}
        for v in self.dataset.variables:
            t = alias_target(v)
            if t and t not in names:
                raise ValueError(f"{self.dataset.id} variable {v.name!r}: alias_of {t!r} is not a variable "
                                 "read from the file")
        return out

    def source_name(self, name: str) -> str:
        v = self.variable(name)
        src = _extra(v, "source_name")
        if not src:
            raise KeyError(f"{self.dataset.id} variable {name!r} needs 'source_name' (HDF5 path) in its config")
        return str(src).lstrip("/")

    def sources(self) -> list[str]:
        return [self.source_name(v.name) for v in self.source_variables()]

    def request_paths(self) -> list[str]:
        """HDF5 paths requested from OPeNDAP (product specific)."""
        return self.sources()

    def window(self, aoi: AOI) -> tuple[int, int, int, int]:
        return aoi_window(aoi, self.buffer_cells)

    # ------------------------------------------------------------------ product hooks
    def _pattern_params(self) -> dict[str, Any]:
        return {"granule_ur[]": [self.file_pattern], "options[granule_ur][pattern]": "true"}

    def select(self, items: Iterable[Mapping[str, Any]], first: date | None = None,
               last: date | None = None) -> list[SmapGranule]:
        raise NotImplementedError

    def slots_per_day(self) -> int:
        return 1

    def unit_meta(self) -> dict[str, Any]:
        return {}

    def subset_url(self, granule: Mapping[str, Any], window, paths: Sequence[str]) -> str:
        raise NotImplementedError

    def read_subset(self, src: Any, paths: Sequence[str]) -> dict[str, Any]:
        raise NotImplementedError

    def check(self, sub: Mapping[str, Any], window, paths: Sequence[str]) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ sessions (overridden in tests)
    def cmr_session(self):
        return ed.cmr_session(self.settings)

    def opendap_session(self):
        """Per-thread ``requests`` session with the bearer token (only used for :data:`TOKEN_HOSTS`)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = self.http_session(TOKEN_ENV)
            self._local.session = s
        return s

    # ------------------------------------------------------------------ discover
    def latest_day(self, session=None) -> date | None:
        """Day of the newest granule (one CMR query, newest first)."""
        s = session or self.cmr_session()
        params = {"collection_concept_id": self.concept_id, "sort_key": "-start_date", "page_size": 20,
                  **self._pattern_params()}
        r = s.get(ed.CMR_GRANULES_URL, params=params, timeout=self.settings.http_timeout)
        r.raise_for_status()
        grans = self.select(r.json().get("items", []))
        return max((g.day for g in grans), default=None)

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        sources = self.sources()
        session = self.cmr_session()
        latest = self.latest_day(session)
        if latest is None:
            log.warning("%s: CMR lists no granule of %s", self.dataset.id, self.concept_id)
            return []
        months = month_starts(period)
        q_start, q_end = months[0], min(month_end(months[-1]), latest)
        if q_end < q_start:
            log.info("%s %s: nothing available yet (data end %s)", self.dataset.id, aoi.id, latest)
            return []
        items = ed.search_granules(session, self.concept_id, bbox=aoi.bbox, start=q_start, end=q_end,
                                   params=self._pattern_params(), page_size=2000, max_pages=50,
                                   timeout=self.settings.http_timeout)
        granules = self.select(items, first=q_start, last=q_end)
        window = self.window(aoi)
        refs: list[SourceRef] = []
        for first in months:
            last_cal = month_end(first)
            grans = [g for g in granules if first <= g.day <= last_cal]
            if not grans:
                if first <= latest:
                    log.warning("%s %s: no granules in %s", self.dataset.id, aoi.id, f"{first:%Y-%m}")
                continue
            complete = last_cal <= latest
            last = max(g.day for g in grans)
            stem = f"{self.dataset.id}-{first:%Y%m}" + ("" if complete else f"-through-{last:%Y%m%d}")
            key = f"{stem}-{granule_digest(grans)}"
            days_expected = (min(last_cal, latest) - first).days + 1
            refs.append(self.ref(
                key, utc_midnight(first), [g.opendap for g in grans],
                month=f"{first:%Y-%m}", first_day=first.isoformat(), last_day=last.isoformat(),
                complete=complete, data_end=latest.isoformat(), **self.unit_meta(),
                granules=[asdict(g) for g in grans], versions=sorted({g.version for g in grans}),
                n_granules=len(grans), expected_granules=days_expected * self.slots_per_day(),
                window=list(window), variables=[v.name for v in self.dataset.variables], sources=sources,
                collection=self.concept_id))
        return refs

    # ------------------------------------------------------------------ extract
    def raw_rel(self, ref: SourceRef, aoi: AOI, granule: Mapping[str, Any]) -> str:
        return self.raw_path(ref, f"{granule['stem']}__{aoi.id}.nc4")

    def missing_credentials(self, aoi: AOI, ref: SourceRef | None = None) -> list[str]:
        """CMR discovery is public; a unit needs ``EARTHDATA_TOKEN`` unless all its subsets are in ``raw/``."""
        if ref is None:
            return []
        if all(self.lake.exists(self.raw_rel(ref, aoi, g)) for g in ref.meta.get("granules", [])):
            return []
        return [] if self.settings.has_secret(TOKEN_ENV) else [TOKEN_ENV]

    def _cached_ok(self, rel: str, window, sources=None) -> bool:
        if not self.lake.exists(rel):
            return False
        paths = list(sources) if sources is not None else self.request_paths()
        try:
            with self.lake.local_path(rel, "r") as p:
                self.check(self.read_subset(p, paths), window, paths)
            return True
        except Exception as exc:   # stale window, other variables, truncated file: fetch again
            log.info("re-fetching %s: %s", rel, exc)
            return False

    def fetch_subset(self, granule: Mapping[str, Any], window, sources=None) -> bytes:
        """One OPeNDAP subset (retries on 429/5xx and connection errors); returns the checked netCDF-4 bytes."""
        import requests

        paths = list(sources) if sources is not None else self.request_paths()
        url = self.subset_url(granule, window, paths)
        if not wants_token(url):
            raise SmapAuthError(f"refusing to send the Earthdata token to {urlsplit(url).netloc}")
        if not self.settings.has_secret(TOKEN_ENV):
            raise SmapAuthError(f"{TOKEN_ENV} is not set: the SMAP OPeNDAP subsets need an Earthdata Login bearer "
                                "token (https://urs.earthdata.nasa.gov/profile -> Generate Token)")
        last: str = ""
        for attempt in range(self.retries + 1):
            try:
                r = self.opendap_session().get(url, timeout=self.request_timeout)
            except requests.RequestException as exc:
                last = redact(f"{type(exc).__name__}: {exc}")
            else:
                if r.status_code in (401, 403):
                    raise SmapAuthError(f"HTTP {r.status_code} from OPeNDAP for {granule['stem']}: token rejected")
                if r.status_code == 200:
                    body = r.content
                    if body[:8] != ed.HDF5_SIGNATURE:
                        hint = " (HTML page: login redirect?)" if body.lstrip()[:1] == b"<" else ""
                        raise SmapAuthError(f"OPeNDAP did not return netCDF-4 for {granule['stem']}{hint}")
                    self.check(self.read_subset(io.BytesIO(body), paths), window, paths)
                    return body
                last = f"HTTP {r.status_code}: {redact(r.text[:200])}"
                if r.status_code not in (408, 429) and r.status_code < 500:
                    break
            if attempt < self.retries:
                _time.sleep(2.0 * 2 ** attempt)
        raise RuntimeError(f"OPeNDAP subset failed for {granule['stem']}: {last}")

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        window = self.window(aoi)
        paths = self.request_paths()
        out, todo = [], []
        for g in ref.meta["granules"]:
            rel = self.raw_rel(ref, aoi, g)
            out.append(rel)
            if not self._cached_ok(rel, window, paths):
                todo.append((g, rel))
        if not todo:
            return out

        def work(job) -> int:
            g, rel = job
            body = self.fetch_subset(g, window, paths)
            self.lake.write_bytes(rel, body)
            return len(body)

        t0 = _time.time()
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(todo))) as pool:
            sizes = list(pool.map(work, todo))
        log.info("%s %s %s: fetched %d subsets, %.2f MB in %.0f s", self.dataset.id, aoi.id, ref.meta["month"],
                 len(sizes), sum(sizes) / 1e6, _time.time() - t0)
        return out

    # ------------------------------------------------------------------ transform helpers
    def _finish_variables(self, stacks: dict[str, list[np.ndarray]], order: np.ndarray) -> tuple[dict, int, int]:
        """Stack, mask ``valid_range`` and build the data variables (source variables first, aliases as copies).
        Returns ``(data_vars, masked, n_valid)`` counted over the source variables only."""
        import xarray as xr

        data_vars: dict[str, Any] = {}
        finals: dict[str, np.ndarray] = {}
        masked = n_valid = 0
        for v in self.source_variables():
            arr = np.stack(stacks[v.name])[order]
            nv = int(np.isfinite(arr).sum())
            vals, frac = mask_out_of_range(arr, v.valid_range)
            masked += int(round(frac * nv))
            n_valid += nv
            finals[v.name] = vals.astype("float32")
        for v in self.dataset.variables:
            target = alias_target(v)
            vals = finals[target].copy() if target else finals[v.name]
            if target and v.valid_range:
                vals, _ = mask_out_of_range(vals, v.valid_range)
            attrs = {"units": v.unit or "", "long_name": v.description or v.name,
                     "valid_range": list(v.valid_range) if v.valid_range else []}
            attrs.update(self._variable_attrs(v, target))
            data_vars[v.name] = xr.DataArray(vals.astype("float32"), dims=("time", "y", "x"), attrs=attrs)
        return data_vars, masked, n_valid

    def _variable_attrs(self, v: Any, target: str | None) -> dict[str, Any]:
        if target:
            return {"fuellake_alias_of": target,
                    "smap_source_variable": f"/{self.source_name(target)}"}
        return {"smap_source_variable": f"/{self.source_name(v.name)}"}


class SmapL4Connector(_SmapDailyConnector):
    """SMAP L4 SPL4SMGP daily samples for one AOI, one Zarr item per calendar month (native 9 km grid)."""

    default_concept_id = DEFAULT_CONCEPT_ID

    @property
    def sample_times(self) -> list[str]:
        return list(self.dataset.options.get("sample_times_utc", ["13:30"]))

    # ------------------------------------------------------------------ product hooks
    def _pattern_params(self) -> dict[str, Any]:
        return {"granule_ur[]": [f"SMAP_L4_SM_gph_*T{t}_*" for t in sample_time_tokens(self.sample_times)],
                "options[granule_ur][pattern]": "true"}

    def select(self, items, first=None, last=None) -> list[SmapGranule]:
        return select_granules(items, self.sample_times, first=first, last=last)

    def slots_per_day(self) -> int:
        return len(self.sample_times)

    def unit_meta(self) -> dict[str, Any]:
        return {"sample_times_utc": self.sample_times}

    def subset_url(self, granule, window, paths) -> str:
        return dap4_url(granule["opendap"], paths, window)

    def read_subset(self, src, paths) -> dict[str, Any]:
        return read_dap_subset(src, paths)

    def check(self, sub, window, paths) -> None:
        check_subset(sub, window, paths, self.georef_tolerance_m)

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        if not raw:
            return []
        granules = list(ref.meta["granules"])
        if len(raw) != len(granules):
            raise RuntimeError(f"{self.dataset.id}: expected {len(granules)} raw subsets, got {len(raw)}")
        window = self.window(aoi)
        variables = self.source_variables()
        sources = [self.source_name(v.name) for v in variables]
        stacks: dict[str, list[np.ndarray]] = {v.name: [] for v in variables}
        times: list[np.datetime64] = []
        raw_bytes = 0
        source_units: dict[str, str] = {}
        for g, rel in zip(granules, raw, strict=True):
            with self.lake.local_path(rel, "r") as p:
                sub = read_dap_subset(p, sources)
            raw_bytes += len(self.lake.read_bytes(rel))
            check_subset(sub, window, sources, self.georef_tolerance_m)
            nominal = datetime.fromisoformat(g["time"])
            if abs((subset_time(sub["time"]) - nominal).total_seconds()) > TIME_TOLERANCE_S:
                raise ValueError(f"{g['stem']}: /time {subset_time(sub['time'])} does not match the file name "
                                 f"time {nominal}")
            for v, s in zip(variables, sources, strict=True):
                stacks[v.name].append(layer_values(sub, s))
                if "units" in sub["attrs"].get(s, {}):
                    source_units[v.name] = sub["attrs"][s]["units"]
            times.append(np.datetime64(nominal.astimezone(UTC).replace(tzinfo=None), "ns"))
        order = np.argsort(np.array(times))
        self._source_units = source_units
        data_vars, masked, n_valid = self._finish_variables(stacks, order)
        ds = _xy_dataset(data_vars, np.array(times, dtype="datetime64[ns]")[order], window)
        days = sorted({g_day.astype("datetime64[D]").item() for g_day in ds["time"].values})
        first = date.fromisoformat(ref.meta["first_day"])
        last = days[-1]
        missing_days = sorted(d.isoformat() for d in set(_days_through(first, last)) - set(days))
        versions = sorted({g["version"] for g in granules})
        ds.attrs.update({k: str(v) for k, v in {
            "title": "SMAP L4 SPL4SMGP surface, root-zone and profile soil moisture (AOI subset, native grid)",
            "source": f"NASA NSIDC DAAC via OPeNDAP in Earthdata Cloud, CMR collection {self.concept_id}",
            "citation": CITATION,
            "license": LICENSE_TEXT,
            "time_convention": "time = nominal centre of the 3-hour time-average window (file name), UTC",
            "sample_times_utc": ",".join(self.sample_times),
            "grid": f"EASE-Grid 2.0 Global 9 km (EPSG:{EASE2_EPSG}), {EASE2_9KM_CELL:.4f} m cells, "
                    f"global rows {window[0]}..{window[1] - 1}, cols {window[2]}..{window[3] - 1}",
            "smap_version_ids": ",".join(versions),
            "fuellake_aoi": aoi.id,
        }.items()})
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        valid = np.isfinite(ds[variables[0].name].values)
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": (masked / n_valid) if n_valid else 0.0,
            "fuellake:source_ids": [g["ur"] for g in granules],
            "smap:product": "SPL4SMGP",
            "smap:collection": self.concept_id,
            "smap:version": str(self.dataset.options.get("version", "008")),
            "smap:version_ids": versions,
            "smap:sample_times_utc": self.sample_times,
            "smap:first_day": first.isoformat(),
            "smap:last_day": last.isoformat(),
            "smap:complete_month": bool(ref.meta.get("complete")),
            "smap:data_end": ref.meta.get("data_end"),
            "smap:n_granules": len(granules),
            "smap:missing_days": missing_days,
            "smap:variables": [v.name for v in self.dataset.variables],
            "smap:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])],
            "smap:window": list(window),
            "smap:grid": f"EASE-Grid 2.0 Global 9 km, EPSG:{EASE2_EPSG}, {EASE2_9KM_CELL:.4f} m",
            "smap:valid_fraction": float(valid.mean()) if valid.size else 0.0,
            "smap:raw_bytes": int(raw_bytes),
        }
        return [StagedItem(item_id=iid, datetime=utc_midnight(first), properties=props,
                           assets=[self.zarr_asset(path)], bbox=window_bounds_wgs84(window),
                           start_datetime=utc_midnight(first),
                           end_datetime=utc_midnight(last + timedelta(days=1)) - timedelta(microseconds=1))]

    def _variable_attrs(self, v: Any, target: str | None) -> dict[str, Any]:
        attrs = super()._variable_attrs(v, target)
        attrs["smap_source_units"] = getattr(self, "_source_units", {}).get(target or v.name, "")
        return attrs


class SmapL3eConnector(_SmapDailyConnector):
    """SMAP enhanced L3 passive soil moisture SPL3SMP_E (AM / PM retrievals) for one AOI, one Zarr item per
    calendar month (native 9 km grid, one time step per UTC day)."""

    default_concept_id = L3E_CONCEPT_ID
    file_pattern = "SMAP_L3_SM_P_E_*"

    @property
    def index_paths(self) -> list[tuple[str, str]]:
        pairs = self.dataset.options.get("georef_index_paths") or [list(p) for p in L3E_INDEX_PATHS]
        return [(str(a).lstrip("/"), str(b).lstrip("/")) for a, b in pairs]

    def quality(self, name: str) -> tuple[str | None, int]:
        """``(quality flag path, reject bits)`` of a source variable (``(None, 0)``: every valid value)."""
        v = self.variable(name)
        flag = _extra(v, "quality_flag")
        bits = _extra(v, "reject_bits")
        if flag and bits is None:
            bits = 1
        return (str(flag).lstrip("/") if flag else None), int(bits or 0)

    def request_paths(self) -> list[str]:
        paths: list[str] = []
        for v in self.source_variables():
            flag, _ = self.quality(v.name)
            paths += [self.source_name(v.name)] + ([flag] if flag else [])
        for pair in self.index_paths:
            paths += list(pair)
        return list(dict.fromkeys(paths))

    # ------------------------------------------------------------------ product hooks
    def select(self, items, first=None, last=None) -> list[SmapGranule]:
        return select_l3e_granules(items, first=first, last=last)

    def subset_url(self, granule, window, paths) -> str:
        return dap4_url_2d(granule["opendap"], paths, window)

    def read_subset(self, src, paths) -> dict[str, Any]:
        return read_l3e_subset(src, paths)

    def check(self, sub, window, paths) -> None:
        check_l3e_subset(sub, window, paths, self.index_paths)

    def _variable_attrs(self, v: Any, target: str | None) -> dict[str, Any]:
        attrs = super()._variable_attrs(v, target)
        flag, bits = self.quality(target or v.name)
        attrs.update({"smap_source_units": "cm**3/cm**3 (= m3/m3)", "smap_quality_flag": f"/{flag}" if flag else "",
                      "smap_reject_bits": int(bits)})
        return attrs

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        if not raw:
            return []
        granules = list(ref.meta["granules"])
        if len(raw) != len(granules):
            raise RuntimeError(f"{self.dataset.id}: expected {len(granules)} raw subsets, got {len(raw)}")
        window = self.window(aoi)
        variables = self.source_variables()
        paths = self.request_paths()
        stacks: dict[str, list[np.ndarray]] = {v.name: [] for v in variables}
        rejected = dict.fromkeys((v.name for v in variables), 0)
        before = dict.fromkeys((v.name for v in variables), 0)
        times: list[np.datetime64] = []
        raw_bytes = 0
        for g, rel in zip(granules, raw, strict=True):
            with self.lake.local_path(rel, "r") as p:
                sub = read_l3e_subset(p, paths)
            raw_bytes += len(self.lake.read_bytes(rel))
            check_l3e_subset(sub, window, paths, self.index_paths)
            for v in variables:
                flag, bits = self.quality(v.name)
                vals, n_rej = quality_masked(sub, self.source_name(v.name), flag, bits)
                stacks[v.name].append(vals)
                rejected[v.name] += n_rej
                before[v.name] += int(np.isfinite(vals).sum()) + n_rej
            day = datetime.fromisoformat(g["time"]).astimezone(UTC)
            times.append(np.datetime64(day.replace(tzinfo=None), "ns"))
        order = np.argsort(np.array(times))
        data_vars, masked, n_valid = self._finish_variables(stacks, order)
        ds = _xy_dataset(data_vars, np.array(times, dtype="datetime64[ns]")[order], window)
        days = sorted({t.astype("datetime64[D]").item() for t in ds["time"].values})
        first = date.fromisoformat(ref.meta["first_day"])
        last = days[-1]
        missing_days = sorted(d.isoformat() for d in set(_days_through(first, last)) - set(days))
        crids = sorted({g["version"] for g in granules})
        ds.attrs.update({k: str(v) for k, v in {
            "title": "SMAP enhanced L3 passive soil moisture SPL3SMP_E, AM and PM retrievals (AOI subset, native grid)",
            "source": f"NASA NSIDC DAAC via OPeNDAP in Earthdata Cloud, CMR collection {self.concept_id}",
            "citation": L3E_CITATION,
            "license": LICENSE_TEXT,
            "time_convention": "time = 00:00 UTC of the daily composite's UTC day; AM retrievals were observed at "
                               "about 14:00 UTC of that day over California (6 a.m. local, descending), PM "
                               "retrievals at about 02:00 UTC (6 p.m. local of the previous day, ascending)",
            "grid": f"EASE-Grid 2.0 Global 9 km (EPSG:{EASE2_EPSG}), {EASE2_9KM_CELL:.4f} m cells, "
                    f"global rows {window[0]}..{window[1] - 1}, cols {window[2]}..{window[3] - 1}",
            "smap_crids": ",".join(crids),
            "fuellake_aoi": aoi.id,
        }.items()})
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        valid_fractions = {name: float(np.isfinite(ds[name].values).mean()) for name in ds.data_vars}
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": (masked / n_valid) if n_valid else 0.0,
            "fuellake:source_ids": [g["ur"] for g in granules],
            "smap:product": "SPL3SMP_E",
            "smap:collection": self.concept_id,
            "smap:version": str(self.dataset.options.get("version", "006")),
            "smap:crids": crids,
            "smap:first_day": first.isoformat(),
            "smap:last_day": last.isoformat(),
            "smap:complete_month": bool(ref.meta.get("complete")),
            "smap:data_end": ref.meta.get("data_end"),
            "smap:n_granules": len(granules),
            "smap:missing_days": missing_days,
            "smap:variables": [v.name for v in self.dataset.variables],
            "smap:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])],
            "smap:window": list(window),
            "smap:grid": f"EASE-Grid 2.0 Global 9 km, EPSG:{EASE2_EPSG}, {EASE2_9KM_CELL:.4f} m",
            "smap:valid_fractions": valid_fractions,
            "smap:quality_rejected_fraction": {k: (rejected[k] / before[k]) if before[k] else 0.0
                                               for k in rejected},
            "smap:raw_bytes": int(raw_bytes),
        }
        return [StagedItem(item_id=iid, datetime=utc_midnight(first), properties=props,
                           assets=[self.zarr_asset(path)], bbox=window_bounds_wgs84(window),
                           start_datetime=utc_midnight(first),
                           end_datetime=utc_midnight(last + timedelta(days=1)) - timedelta(microseconds=1))]


# ---------------------------------------------------------------------------- climatology
N_STEPS = 366


def climatology_step(d: date | datetime) -> int:
    """0-based step of ``d`` on the 366-day leap reference year (same month and day: 29 Feb = 59,
    1 Mar = 60 in every year)."""
    return (date(2000, d.month, d.day) - date(2000, 1, 1)).days


def doy_climatology(values: np.ndarray, days: Sequence[date], window_days: int = 31,
                    min_count: int = 1) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Day-of-year climatology of daily fields ``values`` ``(time, y, x)`` (NaN = no value) on ``days``.

    Returns ``(mean, std, count)``, each ``(366, y, x)`` float32: for every step of the leap reference year
    the mean, sample standard deviation (ddof 1) and number of the valid daily values whose circular day
    distance to that step is at most ``window_days // 2``. Mean and std are NaN where the count is below
    ``max(min_count, 1)`` (std also where the count is below 2)."""
    vals = np.asarray(values, dtype="float64")
    if vals.ndim != 3 or vals.shape[0] != len(days):
        raise ValueError(f"values {vals.shape} do not match {len(days)} days")
    if window_days < 1 or window_days % 2 == 0 or window_days > N_STEPS:
        raise ValueError(f"window_days must be an odd number in 1..{N_STEPS}, got {window_days}")
    shape = (N_STEPS,) + vals.shape[1:]
    s1, s2, n = np.zeros(shape), np.zeros(shape), np.zeros(shape)
    steps = np.array([climatology_step(d) for d in days], dtype="int64")
    for k in np.unique(steps):
        block = vals[steps == k]
        ok = np.isfinite(block)
        b = np.where(ok, block, 0.0)
        s1[k] += b.sum(axis=0)
        s2[k] += (b * b).sum(axis=0)
        n[k] += ok.sum(axis=0)
    half = window_days // 2
    w1, w2, wn = np.zeros(shape), np.zeros(shape), np.zeros(shape)
    for off in range(-half, half + 1):
        w1 += np.roll(s1, -off, axis=0)
        w2 += np.roll(s2, -off, axis=0)
        wn += np.roll(n, -off, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = w1 / wn
        var = (w2 - w1 * mean) / (wn - 1.0)
    std = np.sqrt(np.clip(var, 0.0, None))
    low = wn < max(int(min_count), 1)
    mean[low] = np.nan
    std[low | (wn < 2)] = np.nan
    return mean.astype("float32"), std.astype("float32"), wn.astype("float32")


def _store_days(ds, name: str) -> tuple[np.ndarray, list[date]]:
    """Daily means of ``ds[name]`` (the mean of each UTC day's time steps) and their days."""
    import pandas as pd

    arr = np.asarray(ds[name].values, dtype="float32")
    t = pd.DatetimeIndex(ds["time"].values)
    days = [ts.date() for ts in t]
    uniq = sorted(set(days))
    if len(uniq) == len(days):
        order = np.argsort(np.array(days, dtype="datetime64[D]"))
        return arr[order], [days[i] for i in order]
    out = np.empty((len(uniq),) + arr.shape[1:], dtype="float32")
    with np.errstate(invalid="ignore"):
        for i, d in enumerate(uniq):
            sel = arr[[j for j, dd in enumerate(days) if dd == d]]
            out[i] = np.nanmean(sel, axis=0) if np.isfinite(sel).any() else np.nan
    return out, uniq


class SmapClimatologyConnector(BaseConnector):
    """Day-of-year climatology (window mean, std, count per 9 km cell) of cataloged SMAP form B items; one
    item per AOI, dated in the reference year (see the module docstring). Reads only the lake."""

    # ------------------------------------------------------------------ config
    @property
    def baseline(self) -> tuple[date, date]:
        b = self.dataset.options.get("baseline", ["2015-10-01", "2024-12-31"])
        start, end = (date.fromisoformat(str(v)) for v in b)
        if end < start:
            raise ValueError(f"{self.dataset.id}: baseline end {end} before start {start}")
        return start, end

    @property
    def window_days(self) -> int:
        return int(self.dataset.options.get("window_days", 31))

    @property
    def min_count(self) -> int:
        return int(self.dataset.options.get("min_count", 1))

    @property
    def reference_year(self) -> int:
        y = int(self.dataset.options.get("reference_year", 1904))
        if not (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
            raise ValueError(f"{self.dataset.id}: reference_year {y} is not a leap year")
        return y

    def inputs(self) -> list[dict[str, str]]:
        """``options.inputs``: ``[{dataset, variable, prefix}]`` (prefix of the output variables)."""
        out = []
        for inp in self.dataset.options.get("inputs") or []:
            d = {"dataset": str(inp["dataset"]), "variable": str(inp["variable"]),
                 "prefix": str(inp.get("prefix") or inp["variable"])}
            for stat in ("mean", "std", "count"):
                self.variable(f"{d['prefix']}_{stat}")     # every output must be declared in the config
            out.append(d)
        if not out:
            raise ValueError(f"{self.dataset.id}: options.inputs is empty")
        return out

    # ------------------------------------------------------------------ discover
    def _month_items(self, aoi: AOI, inp: Mapping[str, str]) -> tuple[list[dict], list[str]]:
        """The baseline month items of one input (one per month) and the months missing or incomplete."""
        from fuellake import catalog

        start, end = self.baseline
        items = catalog.search(self.lake, [inp["dataset"]], aoi=aoi.id, start=start, end=end)
        by_month: dict[str, dict] = {}
        for it in items:
            p = it["properties"]
            first = p.get("smap:first_day") or str(p.get("start_datetime") or p["datetime"])[:10]
            by_month[first[:7]] = it
        chosen, problems = [], []
        for m in month_starts(Period(start, end)):
            it = by_month.get(f"{m:%Y-%m}")
            need = min(month_end(m), end)
            if it is None:
                problems.append(f"{m:%Y-%m} missing")
                continue
            p = it["properties"]
            last = date.fromisoformat(str(p.get("smap:last_day") or str(p.get("end_datetime"))[:10]))
            if not (p.get("smap:complete_month") or last >= need):
                problems.append(f"{m:%Y-%m} incomplete (through {last})")
                continue
            if inp["variable"] not in (p.get("smap:variables") or [inp["variable"]]):
                problems.append(f"{m:%Y-%m} has no variable {inp['variable']}")
                continue
            chosen.append(it)
        return chosen, problems

    def discover(self, aoi: AOI, period: Period) -> list[SourceRef]:
        """One unit per AOI whatever ``period`` (a static layer); none while an input is incomplete."""
        from fuellake.catalog import asset_rel

        start, end = self.baseline
        metas, windows, keys = [], set(), []
        for inp in self.inputs():
            items, problems = self._month_items(aoi, inp)
            if problems:
                log.warning("%s %s: input %s/%s is not complete for the baseline %s..%s (%d month(s): %s); "
                            "run it first", self.dataset.id, aoi.id, inp["dataset"], inp["variable"], start, end,
                            len(problems), ", ".join(problems[:5]) + (" ..." if len(problems) > 5 else ""))
                return []
            for it in items:
                windows.add(tuple(it["properties"].get("smap:window") or ()))
                keys.append(f"{inp['dataset']}:{it['id']}={it['properties'].get('fuellake:source_key')}")
            metas.append({**inp, "items": [{"id": it["id"], "rel": asset_rel(it, "data"),
                                            "source_key": it["properties"].get("fuellake:source_key")}
                                           for it in items]})
        if len(windows) != 1 or not next(iter(windows)):
            log.warning("%s %s: input items do not share one smap:window (%s); rerun the inputs on one AOI grid",
                        self.dataset.id, aoi.id, sorted(windows))
            return []
        window = list(next(iter(windows)))
        digest = hashlib.sha1("\n".join(sorted(keys)).encode()).hexdigest()[:8]
        key = f"{self.dataset.id}-{start:%Y%m%d}-{end:%Y%m%d}-w{self.window_days}-{digest}"
        return [self.ref(key, datetime(self.reference_year, 1, 1, tzinfo=UTC), [], inputs=metas, window=window,
                         baseline=[start.isoformat(), end.isoformat()], window_days=self.window_days,
                         min_count=self.min_count, reference_year=self.reference_year)]

    def extract(self, ref: SourceRef, aoi: AOI) -> list[str]:
        return []      # derived from staged items: nothing to download

    # ------------------------------------------------------------------ transform
    def transform(self, ref: SourceRef, raw: list[str], aoi: AOI) -> list[StagedItem]:
        import pandas as pd
        import xarray as xr

        from fuellake.rasters import open_zarr

        start, end = (date.fromisoformat(v) for v in ref.meta["baseline"])
        window = [int(v) for v in ref.meta["window"]]
        xe, ye = window_centres(window)
        year = int(ref.meta["reference_year"])
        times = pd.date_range(f"{year}-01-01", periods=N_STEPS, freq="D").values.astype("datetime64[ns]")
        data_vars: dict[str, Any] = {}
        inputs_prop, source_ids = [], []
        for inp in ref.meta["inputs"]:
            arrays, days = [], []
            for it in inp["items"]:
                ds = open_zarr(self.lake, it["rel"])
                if not (np.allclose(ds["x"].values, xe, atol=1e-3) and np.allclose(ds["y"].values, ye, atol=1e-3)):
                    raise ValueError(f"{it['id']}: x/y differ from the analytic centres of window {tuple(window)}")
                a, d = _store_days(ds, inp["variable"])
                keep = [i for i, dd in enumerate(d) if start <= dd <= end]
                arrays.append(a[keep])
                days += [d[i] for i in keep]
                source_ids.append(it["id"])
            values = np.concatenate(arrays, axis=0)
            if len(set(days)) != len(days):
                raise ValueError(f"{inp['dataset']}: a day appears in two input items")
            mean, std, count = doy_climatology(values, days, self.window_days, self.min_count)
            years: dict[str, int] = {}
            for mth in range(1, 13):
                years[f"{mth:02d}"] = len({dd.year for dd in days if dd.month == mth})
            for stat, arr, unit in (("mean", mean, "m3/m3"), ("std", std, "m3/m3"), ("count", count, "1")):
                v = self.variable(f"{inp['prefix']}_{stat}")
                data_vars[v.name] = xr.DataArray(arr, dims=("time", "y", "x"), attrs={
                    "units": v.unit or unit, "long_name": v.description or v.name,
                    "fuellake_input": f"{inp['dataset']}/{inp['variable']}", "statistic": stat})
            inputs_prop.append({"dataset": inp["dataset"], "variable": inp["variable"], "prefix": inp["prefix"],
                                "n_items": len(inp["items"]), "n_days": len(days),
                                "n_valid_values": int(np.isfinite(values).sum()),
                                "first_day": min(days).isoformat(), "last_day": max(days).isoformat(),
                                "years_by_month": years})
        ds = _xy_dataset(data_vars, times, window)
        ds = ds.assign_coords(dayofyear=("time", np.arange(1, N_STEPS + 1, dtype="int16")))
        ds["dayofyear"].attrs.update({"long_name": "day of the leap reference year (1 Mar = 61 in every year)"})
        method = (f"{self.window_days}-day circular window around each day of a leap reference year; daily values "
                  f"{start}..{end} pooled across years; mean, sample std (ddof 1), count; mean/std NaN where "
                  f"count < {self.min_count}; 29 Feb only from leap years")
        ds.attrs.update({k: str(v) for k, v in {
            "title": "SMAP soil moisture day-of-year climatology (window mean, std, count; native 9 km grid)",
            "source": "fuel-lake, computed from the cataloged items of " + ", ".join(
                sorted({i["dataset"] for i in ref.meta["inputs"]})),
            "citation": f"{CITATION} | {L3E_CITATION}",
            "license": LICENSE_TEXT,
            "method": method,
            "baseline": f"{start}..{end}",
            "time_convention": f"time = the 366 days of the reference year {year} (a leap year standing for any "
                               "year): look a date up by month and day (fuellake.connectors.smap.climatology_step)",
            "grid": f"EASE-Grid 2.0 Global 9 km (EPSG:{EASE2_EPSG}), {EASE2_9KM_CELL:.4f} m cells, "
                    f"global rows {window[0]}..{window[1] - 1}, cols {window[2]}..{window[3] - 1}",
            "fuellake_aoi": aoi.id,
        }.items()})
        iid = self.item_id(aoi, ref)
        path = self.zarr_path(aoi, iid)
        write_zarr(self.lake, path, ds)
        t0 = datetime(year, 1, 1, tzinfo=UTC)
        props: dict[str, Any] = {
            "fuellake:qc_masked_fraction": 0.0,
            "fuellake:source_ids": list(dict.fromkeys(source_ids)),
            "smap_clim:baseline": [start.isoformat(), end.isoformat()],
            "smap_clim:window_days": self.window_days,
            "smap_clim:min_count": self.min_count,
            "smap_clim:std_ddof": 1,
            "smap_clim:reference_year": year,
            "smap_clim:method": method,
            "smap_clim:inputs": inputs_prop,
            "smap:window": window,
            "smap:grid": f"EASE-Grid 2.0 Global 9 km, EPSG:{EASE2_EPSG}, {EASE2_9KM_CELL:.4f} m",
            "smap:shape": [int(ds.sizes["time"]), int(ds.sizes["y"]), int(ds.sizes["x"])],
            "smap:variables": list(data_vars),
        }
        return [StagedItem(item_id=iid, datetime=t0, properties=props, assets=[self.zarr_asset(path)],
                           bbox=window_bounds_wgs84(window), start_datetime=t0,
                           end_datetime=datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(microseconds=1))]
