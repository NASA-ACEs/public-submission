# fuel-lake — shared NASA Earthdata helpers: CMR search, UMM-G parsing, NISAR names, authenticated reads
# Licensed under Apache-2.0.
"""Helpers shared by the NISAR GCOV, NISAR SME2 and OPERA RTC-S1 connectors.

Search
------
:func:`search_granules` queries the public CMR granule endpoint (``granules.umm_json``) by collection
concept id, AOI bbox and period, paging with the ``CMR-Search-After`` header. CMR search needs no login, so
**no token is sent to CMR**. :func:`parse_granule` turns one UMM-G record into a :class:`Granule`.

NISAR file names follow the convention of the product specifications (GCOV JPL D-102274 Rev E, SME2
JPL D-107677 Rev C, section 2.3)::

    NISAR_<I><L>_<PT>_<PROD>_<CYC>_<REL>_<P>_<FRM>_<MODE>_<POLE>_<S>_<start>_<end>_<CRID>_<A>_<C>_<LOC>_<CTR>

The CRID (composite release id, e.g. ``P05023``) is not a CMR attribute; :func:`parse_nisar_name` reads it
from the name. :func:`dedupe_latest` keeps the highest product counter (``_001`` / ``_002``) per
acquisition. :func:`discover_nisar` is the common NISAR discovery: one collection (= one maturity), the
production pipeline ``PR`` only (no Urgent Response copies), the AOI's preferred stack, AOI coverage.

Authenticated reads
-------------------
Science files at ASF redirect to Earthdata Login unless a bearer token is sent. The token is read from
``EARTHDATA_TOKEN`` at call time (:func:`bearer_token`), sent only to the data hosts, and never logged:
error messages are passed through :func:`fuellake.config.redact`. :func:`open_h5` opens an HDF5 file
over HTTPS with range reads (h5py on an fsspec HTTP file with a block cache, as in the official ASF NISAR
Cookbook streaming notebook) or a local path (tests). :func:`download` fetches small files (OPERA bursts).

Arrays
------
:func:`to_db` converts linear power to dB; :func:`check_linear_power` refuses input that looks like dB
already. :func:`window_from_centres` and :func:`transform_from_centres` turn the 1-D pixel-centre
coordinate vectors of NISAR HDF5 grids into a read window and an affine transform.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import numpy as np
from affine import Affine

from fuellake.config import Settings, redact

if TYPE_CHECKING:
    import requests

    from fuellake.connectors.base import BaseConnector, Period, SourceRef
    from fuellake.registry import AOI

log = logging.getLogger("fuellake.connectors.earthdata")

CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
TOKEN_ENV = "EARTHDATA_TOKEN"
# Hosts that receive the bearer token (https only). CMR is public and never gets it.
DATA_HOST_SUFFIXES = (".earthdatacloud.nasa.gov",)        # DAAC data hosts in Earthdata Cloud
DATA_HOSTS = ("opendap.earthdata.nasa.gov",)              # exact hosts: OPeNDAP in Earthdata Cloud (SMAP L4)
# Public without login (checked 2026-11-14: 303 to a CloudFront URL with A-userid=None, then 206).
PUBLIC_PATH_PREFIXES = ("/NISAR-SAMPLE-DATA/",)
HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
HTTP_BLOCK_SIZE = 16 * 512 * 512      # 4 MiB, as in the ASF NISAR Cookbook streaming notebook


class EarthdataAuthError(RuntimeError):
    """Missing or rejected Earthdata Login credentials (message never contains the token)."""


# ---------------------------------------------------------------------------- CMR search
def cmr_session(settings: Settings) -> requests.Session:
    """Session for CMR searches: ``User-Agent: fuel-lake/0.1`` and no credentials."""
    import requests

    s = requests.Session()
    s.headers.update(settings.http_headers())
    return s


def temporal_param(start: date, end: date) -> str:
    """CMR ``temporal`` value for an inclusive date range (UTC)."""
    return f"{start:%Y-%m-%d}T00:00:00Z,{end:%Y-%m-%d}T23:59:59Z"


def search_granules(session, concept_id: str, *, bbox: Sequence[float], start: date, end: date,
                    params: Mapping[str, Any] | None = None, page_size: int = 2000, max_pages: int = 25,
                    timeout: float = 60.0) -> list[dict]:
    """All UMM-G granule records of ``concept_id`` intersecting ``bbox`` (W, S, E, N) in [start, end].

    Pages with ``CMR-Search-After``. Raises on HTTP errors. Results are sorted by start date.
    """
    query: dict[str, Any] = {
        "collection_concept_id": concept_id,
        "bounding_box": ",".join(f"{v:g}" for v in bbox),
        "temporal": temporal_param(start, end),
        "page_size": page_size,
        "sort_key": "start_date",
    }
    query.update(params or {})
    items: list[dict] = []
    headers: dict[str, str] = {}
    for _ in range(max_pages):
        resp = session.get(CMR_GRANULES_URL, params=query, headers=headers, timeout=timeout)
        resp.raise_for_status()
        page = resp.json().get("items", [])
        items.extend(page)
        after = resp.headers.get("CMR-Search-After")
        hits = int(resp.headers.get("CMR-Hits", len(items)))
        if not page or not after or len(items) >= hits:
            break
        headers = {"CMR-Search-After": after}
    else:
        log.warning("CMR search for %s stopped after %d pages (%d items)", concept_id, max_pages, len(items))
    return items


# ---------------------------------------------------------------------------- UMM-G parsing
def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    # CMR sometimes gives 6+ fractional digits; Python accepts up to 6.
    m = re.match(r"^(.*?\.\d{1,6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass(frozen=True)
class Granule:
    """The parts of a UMM-G record the connectors use."""

    ur: str                                   # GranuleUR (e.g. the file stem)
    concept_id: str
    collection: str                           # collection short name
    start: datetime
    end: datetime | None
    attrs: Mapping[str, tuple[str, ...]]      # AdditionalAttributes name -> values
    urls: tuple[str, ...]                     # https "GET DATA" links
    polygons: tuple[tuple[tuple[float, float], ...], ...]   # (lon, lat) rings of the footprint
    sizes: Mapping[str, int] = field(default_factory=dict)  # file name -> bytes
    platform: str | None = None
    orbit_number: int | None = None
    production_time: datetime | None = None
    pge_version: str | None = None

    def attr(self, name: str, default: str | None = None) -> str | None:
        v = self.attrs.get(name)
        return v[0] if v else default

    def url_endswith(self, suffix: str, exclude: Iterable[str] = ("QA_STATS",)) -> str | None:
        ex = tuple(exclude)
        for u in self.urls:
            if u.endswith(suffix) and not any(e in u for e in ex):
                return u
        return None

    def size_of(self, url_or_name: str) -> int | None:
        return self.sizes.get(url_or_name.rsplit("/", 1)[-1])


def parse_granule(item: Mapping[str, Any]) -> Granule:
    umm = item["umm"]
    meta = item.get("meta", {})
    attrs = {a["Name"]: tuple(str(v) for v in a.get("Values", [])) for a in umm.get("AdditionalAttributes", [])}
    urls = tuple(u["URL"] for u in umm.get("RelatedUrls", [])
                 if u.get("Type") == "GET DATA" and str(u.get("URL", "")).startswith("https://"))
    polys = []
    geom = (umm.get("SpatialExtent", {}).get("HorizontalSpatialDomain", {}).get("Geometry", {}))
    for gp in geom.get("GPolygons", []):
        pts = gp.get("Boundary", {}).get("Points", [])
        polys.append(tuple((float(p["Longitude"]), float(p["Latitude"])) for p in pts))
    for br in geom.get("BoundingRectangles", []):
        w, s, e, n = (float(br[k]) for k in ("WestBoundingCoordinate", "SouthBoundingCoordinate",
                                             "EastBoundingCoordinate", "NorthBoundingCoordinate"))
        polys.append(((w, s), (e, s), (e, n), (w, n), (w, s)))
    rng = umm.get("TemporalExtent", {}).get("RangeDateTime", {})
    dg = umm.get("DataGranule", {})
    sizes = {a["Name"]: int(a["SizeInBytes"]) for a in dg.get("ArchiveAndDistributionInformation", [])
             if "SizeInBytes" in a and "Name" in a}
    platforms = umm.get("Platforms") or [{}]
    orbits = umm.get("OrbitCalculatedSpatialDomains") or [{}]
    orbit = orbits[0].get("OrbitNumber")
    return Granule(
        ur=umm["GranuleUR"],
        concept_id=meta.get("concept-id", ""),
        collection=umm.get("CollectionReference", {}).get("ShortName", ""),
        start=_parse_time(rng.get("BeginningDateTime")),   # type: ignore[arg-type]
        end=_parse_time(rng.get("EndingDateTime")),
        attrs=attrs,
        urls=urls,
        polygons=tuple(polys),
        sizes=sizes,
        platform=platforms[0].get("ShortName"),
        orbit_number=int(orbit) if orbit is not None else None,
        production_time=_parse_time(dg.get("ProductionDateTime")),
        pge_version=(umm.get("PGEVersionClass") or {}).get("PGEVersion"),
    )


def parse_granules(items: Iterable[Mapping[str, Any]]) -> list[Granule]:
    out = []
    for it in items:
        try:
            out.append(parse_granule(it))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping unparsable UMM-G record %s: %s", it.get("meta", {}).get("concept-id"), exc)
    return out


# ---------------------------------------------------------------------------- footprints
def footprint(granule: Granule):
    """Footprint as a shapely geometry in WGS84 (union of the UMM polygons)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polys = [Polygon(r) for r in granule.polygons if len(r) >= 4]
    return unary_union(polys) if polys else None


def aoi_coverage(geom_wgs84, aoi: AOI) -> float:
    """Fraction (0..1) of the AOI bbox area covered by ``geom_wgs84``, measured in the AOI CRS."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import box

    if geom_wgs84 is None or geom_wgs84.is_empty:
        return 0.0
    tr = Transformer.from_crs("EPSG:4326", aoi.crs, always_xy=True)

    def project(g):
        g = shapely.segmentize(g, 0.01)   # densify edges (about 1 km) before projecting
        return shapely.transform(g, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))

    a = project(box(*aoi.bbox))
    f = project(geom_wgs84)
    return float(a.intersection(f).area / a.area) if a.area else 0.0


# ---------------------------------------------------------------------------- NISAR names
NISAR_NAME_RE = re.compile(
    r"^NISAR_(?P<instrument>[LS])(?P<level>[123])_(?P<processing>[A-Z]{2})_(?P<product>[A-Z0-9]{4})_"
    r"(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<direction>[AD])_(?P<frame>\d{3})_(?P<mode>\d{4})_(?P<pole>[A-Z]{4})_"
    r"(?P<source>[AM])_(?P<start>\d{8}T\d{6})_(?P<end>\d{8}T\d{6})_(?P<crid>[A-Z]\d{5})_(?P<accuracy>[A-Z])_"
    r"(?P<coverage>[A-Z])_(?P<location>[A-Z])_(?P<counter>\d{3})(?:\.(?P<ext>.+))?$"
)
POLE_CODES = {"SH": ["HH"], "SV": ["VV"], "DH": ["HH", "HV"], "DV": ["VV", "VH"], "CL": ["LH", "LV"],
              "CR": ["RH", "RV"], "QP": ["HH", "HV", "VV", "VH"], "NA": []}


def parse_nisar_name(name: str) -> dict[str, Any] | None:
    """Fields of a NISAR product name (file name or GranuleUR); ``None`` if it does not match."""
    m = NISAR_NAME_RE.match(name.rsplit("/", 1)[-1])
    if not m:
        return None
    d: dict[str, Any] = m.groupdict()
    for k in ("cycle", "track", "frame", "counter"):
        d[k] = int(d[k])
    d["level"] = int(d["level"])
    d["orbit_state"] = "ascending" if d["direction"] == "A" else "descending"
    d["start_dt"] = datetime.strptime(d["start"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    d["end_dt"] = datetime.strptime(d["end"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    d["primary_polarizations"] = POLE_CODES.get(d["pole"][:2], [])
    d["secondary_polarizations"] = POLE_CODES.get(d["pole"][2:], [])
    d["stack_id"] = f"{d['track']:03d}_{d['direction']}_{d['frame']:03d}"
    return d


def dedupe_latest(items: Iterable[Any], key: Callable[[Any], Any], version: Callable[[Any], Any]) -> list[Any]:
    """Keep, per ``key``, the item with the highest ``version`` (stable order of first appearance)."""
    best: dict[Any, Any] = {}
    order: list[Any] = []
    for it in items:
        k = key(it)
        if k not in best:
            order.append(k)
            best[k] = it
        elif version(it) > version(best[k]):
            best[k] = it
    return [best[k] for k in order]


# ---------------------------------------------------------------------------- NISAR discovery
def _maturity_collection(connector: BaseConnector) -> tuple[str, dict]:
    opts = connector.dataset.options
    maturity = str(opts.get("maturity") or connector.dataset.maturity or "PROVISIONAL").upper()
    colls = opts.get("collections") or {}
    if maturity not in colls or not colls[maturity].get("concept_id"):
        raise ValueError(f"{connector.dataset.id}: no collection configured for maturity {maturity!r} "
                         f"(options.collections has {sorted(colls)})")
    return maturity, dict(colls[maturity])


def discover_nisar(connector: BaseConnector, aoi: AOI, period: Period, product: str) -> list[SourceRef]:
    """Discovery shared by the NISAR connectors (``product`` = ``GCOV`` or ``SME2``).

    One collection is searched, chosen by ``options.maturity`` (default: the dataset ``maturity``), so a
    series never mixes maturities. Filters: production pipeline ``PR`` (drops Urgent Response copies),
    expected CRID of that collection (``collections.<maturity>.crid``) unless ``options.strict_crid`` is
    false, the AOI's preferred stack (``aoi.nisar``) unless ``options.stack: all``, and
    ``options.min_aoi_coverage`` (default 0.5) of the AOI bbox inside the granule footprint. Duplicates
    (product counters ``_001``/``_002``) keep the highest counter.
    """
    opts = connector.dataset.options
    maturity, coll = _maturity_collection(connector)
    session = cmr_session(connector.settings)
    items = search_granules(session, coll["concept_id"], bbox=aoi.bbox, start=period.start, end=period.end)
    granules = parse_granules(items)
    want_crid = coll.get("crid") if opts.get("strict_crid", True) else None
    stack_mode = str(opts.get("stack", "preferred"))
    min_cov = float(opts.get("min_aoi_coverage", 0.5))
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    kept = []
    for g in granules:
        name = parse_nisar_name(g.ur)
        if name is None or name["product"] != product:
            skip("name")
            continue
        if name["processing"] != "PR" or g.attr("PRODUCTION_PIPELINE", "PR") != "PR":
            skip("not PR")
            continue
        if want_crid and name["crid"] != want_crid:
            skip(f"crid {name['crid']}")
            continue
        if stack_mode == "preferred" and aoi.nisar is not None:
            st = aoi.nisar
            if (name["track"], name["frame"], name["orbit_state"]) != (st.track, st.frame, st.direction):
                skip("other stack")
                continue
        cov = aoi_coverage(footprint(g), aoi)
        if cov < min_cov:
            skip("coverage")
            continue
        kept.append((g, name, cov))
    kept = dedupe_latest(kept, key=lambda t: (t[1]["stack_id"], t[1]["start"]), version=lambda t: t[1]["counter"])
    if skipped:
        log.info("%s discover: kept %d of %d granules; skipped %s", connector.dataset.id, len(kept),
                 len(granules), skipped)

    refs = []
    for g, name, cov in kept:
        url = g.url_endswith(".h5")
        if url is None:
            continue
        pols = list(g.attrs.get("FREQUENCY_A_POLARIZATION") or name["primary_polarizations"])
        refs.append(connector.ref(
            g.ur, g.start, urls=[url],
            granule_ur=g.ur, maturity=maturity, collection=g.collection or coll.get("short_name"),
            concept_id=g.concept_id, crid=name["crid"], track=name["track"], frame=name["frame"],
            orbit_state=name["orbit_state"], stack_id=name["stack_id"], mode=name["mode"], pole=name["pole"],
            polarizations=pols, frequencies=list(g.attrs.get("FREQUENCIES", ())),
            bandwidth=g.attr("RANGE_BANDWIDTH_CONCAT") or g.attr("FREQUENCY_A_RANGE_BANDWIDTH"),
            full_frame=g.attr("FULL_FRAME"), joint_observation=g.attr("JOINT_OBSERVATION"),
            product_version=g.attr("PRODUCT_VERSION"), absolute_orbit=g.orbit_number,
            size_bytes=g.size_of(url), aoi_coverage=round(cov, 4), counter=name["counter"],
            accuracy=name["accuracy"], coverage_flag=name["coverage"],
        ))
    check_single_maturity_refs(refs)
    return refs


def check_single_maturity_refs(refs: Iterable[SourceRef], allow_mixed: bool = False) -> set[str]:
    """Apply :func:`fuellake.contracts.check_single_maturity` to discovered refs (``meta['maturity']``)."""
    from fuellake.contracts import check_single_maturity

    return check_single_maturity([{"properties": {"fuellake:maturity": r.meta.get("maturity")}} for r in refs],
                                 allow_mixed=allow_mixed)


def nisar_item_properties(ref: SourceRef, product_type: str, polarizations: Sequence[str]) -> dict[str, Any]:
    """STAC properties required for NISAR items (ARCHITECTURE.md) plus SAR/sat extras."""
    m = ref.meta
    props: dict[str, Any] = {
        "fuellake:maturity": m["maturity"],
        "sat:relative_orbit": int(m["track"]),
        "fuellake:frame": int(m["frame"]),
        "sat:orbit_state": str(m["orbit_state"]).lower(),
        "fuellake:processing_id": m["crid"],
        "sar:polarizations": list(polarizations),
        "sar:frequency_band": "L",
        "sar:instrument_mode": f"{m['mode']} {m['pole']}",
        "sar:product_type": product_type,
        "platform": "nisar",
        "instruments": ["l-sar"],
        "fuellake:source_ids": [m["granule_ur"], *ref.urls],
        "fuellake:aoi_coverage": m.get("aoi_coverage"),
        "nisar:stack_id": m.get("stack_id"),
        "nisar:collection": m.get("collection"),
        "nisar:product_version": m.get("product_version"),
        "nisar:full_frame": m.get("full_frame"),
        "nisar:joint_observation": m.get("joint_observation"),
        "nisar:range_bandwidth_mhz": m.get("bandwidth"),
    }
    if m.get("absolute_orbit") is not None:
        props["sat:absolute_orbit"] = int(m["absolute_orbit"])
    return {k: v for k, v in props.items() if v is not None}


def item_discriminator(ref: SourceRef) -> str:
    """``t013d071`` (track, direction, frame), as in the ARCHITECTURE.md item id example."""
    m = ref.meta
    return f"t{int(m['track']):03d}{str(m['orbit_state'])[0]}{int(m['frame']):03d}"


# ---------------------------------------------------------------------------- auth + IO
def bearer_token(settings: Settings | None = None) -> str | None:
    return (settings or Settings).secret(TOKEN_ENV)


def is_remote(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def wants_token(url: str) -> bool:
    """True for https URLs on the Earthdata data hosts (:data:`DATA_HOSTS` exactly, or a host ending in one of
    :data:`DATA_HOST_SUFFIXES`), except the public sample-data area. Every other host (CMR, URS, CloudFront or S3
    redirects, look-alike domains, plain http) never gets the token."""
    parts = urlsplit(url)
    if parts.scheme != "https" or any(parts.path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
        return False
    host = (parts.hostname or "").lower()
    return host in DATA_HOSTS or any(host.endswith(s) for s in DATA_HOST_SUFFIXES)


def missing_token(settings: Settings, urls: Iterable[str]) -> list[str]:
    """``[EARTHDATA_TOKEN]`` if any of ``urls`` is a data-host URL that needs the token and it is not set,
    else ``[]`` (used by ``missing_credentials``; returns the name only, never the value)."""
    if any(wants_token(u) for u in urls) and not Settings.has_secret(TOKEN_ENV):
        return [TOKEN_ENV]
    return []


def require_token(settings: Settings, url: str) -> str | None:
    """Token for a data-host URL; raises :class:`EarthdataAuthError` if one is needed and missing."""
    if not wants_token(url):
        return None
    token = bearer_token(settings)
    if not token:
        raise EarthdataAuthError(
            f"{TOKEN_ENV} is not set: NISAR/OPERA files at {urlsplit(url).netloc} need an Earthdata Login "
            "bearer token (https://urs.earthdata.nasa.gov/profile -> Generate Token). "
            "Run scripts/probe_earthdata.py to check access.")
    return token


def _local_path(url: str) -> str:
    return url[len("file://"):] if url.startswith("file://") else url


@contextlib.contextmanager
def open_h5(url: str, settings: Settings, block_size: int = HTTP_BLOCK_SIZE) -> Iterator[Any]:
    """Open an HDF5 file read-only: local path / ``file://`` directly, ``https://`` via range reads.

    Remote reads use ``fsspec`` HTTP with ``block_size`` blocks and a block cache, so only the HDF5
    metadata pages and the chunks actually sliced are transferred (NISAR files use paged aggregation).
    aiohttp (3.14, ``client.py``) drops ``Authorization`` on cross-origin redirects, so the bearer token is
    not forwarded to the signed CloudFront/S3 URL the data host redirects to.
    """
    import h5py

    if not is_remote(url):
        with h5py.File(_local_path(url), "r") as f:
            yield f
        return
    import fsspec

    token = require_token(settings, url)
    headers = dict(settings.http_headers())
    if token:
        headers["Authorization"] = f"Bearer {token}"
    fs = fsspec.filesystem("http", headers=headers, skip_instance_cache=True)
    try:
        fobj = fs.open(url, "rb", block_size=block_size, cache_type="blockcache")
    except Exception as exc:   # aiohttp errors may echo request details: redact
        raise EarthdataAuthError(redact(f"cannot open {url}: {type(exc).__name__}: {exc}")) from None
    try:
        head = fobj.read(8)
        if head != HDF5_SIGNATURE:
            snippet = head.lstrip()[:1]
            hint = " (looks like an HTML login page: token missing or rejected)" if snippet == b"<" else ""
            raise EarthdataAuthError(f"{url} did not return an HDF5 file{hint}")
        fobj.seek(0)
        with h5py.File(fobj, "r") as f:
            yield f
        stats = getattr(fobj, "cache", None)
        if stats is not None and hasattr(stats, "total_requested_bytes"):
            log.info("streamed %.1f MB from %s", stats.total_requested_bytes / 1e6, urlsplit(url).path[-60:])
    finally:
        fobj.close()


def download(url: str, dest: str, settings: Settings, timeout: float = 120.0, retries: int = 2) -> int:
    """Download ``url`` to the local file ``dest`` (bearer token on data hosts). Returns bytes written.

    ``requests`` drops the ``Authorization`` header when a redirect leaves the original host, so the token
    never reaches the signed S3/CloudFront URL. HTML responses (login pages) raise
    :class:`EarthdataAuthError`.
    """
    import requests

    token = require_token(settings, url) if is_remote(url) else None
    if not is_remote(url):
        shutil.copyfile(_local_path(url), dest)
        return int(os.path.getsize(dest))
    headers = dict(settings.http_headers())
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
                if resp.status_code in (401, 403):
                    raise EarthdataAuthError(f"HTTP {resp.status_code} for {urlsplit(url).path[-80:]}: token rejected")
                resp.raise_for_status()
                if "text/html" in resp.headers.get("Content-Type", ""):
                    raise EarthdataAuthError(f"{urlsplit(url).path[-80:]} returned HTML (Earthdata Login page?)")
                n = 0
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
                        n += len(chunk)
                return n
        except EarthdataAuthError:
            raise
        except requests.RequestException as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(redact(f"download failed for {urlsplit(url).path[-80:]}: {type(last).__name__}: {last}"))


# ---------------------------------------------------------------------------- arrays
def to_db(linear: np.ndarray) -> np.ndarray:
    """``10 * log10(x)`` as float32; NaN where ``x`` is not finite or ``<= 0``."""
    x = np.asarray(linear, dtype="float64")
    out = np.full(x.shape, np.nan, dtype="float32")
    ok = np.isfinite(x) & (x > 0)
    out[ok] = (10.0 * np.log10(x[ok])).astype("float32")
    return out


def check_linear_power(arr: np.ndarray, name: str, max_negative_fraction: float = 0.01) -> None:
    """Raise if ``arr`` does not look like linear power (too many negative values suggest dB input)."""
    a = np.asarray(arr)
    finite = np.isfinite(a)
    n = int(finite.sum())
    if n == 0:
        return
    neg = int((a[finite] < 0).sum())
    if neg / n > max_negative_fraction:
        raise ValueError(f"{name}: {neg}/{n} finite values are negative; expected linear power (got dB?)")


def transform_from_centres(x0: float, y0: float, dx: float, dy: float) -> Affine:
    """Affine transform of a grid whose first pixel *centre* is ``(x0, y0)`` (``dy`` < 0 for north-up)."""
    return Affine(dx, 0.0, x0 - dx / 2.0, 0.0, dy, y0 - dy / 2.0)


def window_from_centres(xc: np.ndarray, yc: np.ndarray, bounds: Sequence[float], pad: float = 0.0
                        ) -> tuple[slice, slice] | None:
    """Row and column slices of the pixels whose centres fall inside ``bounds`` (minx, miny, maxx, maxy)
    grown by ``pad`` (map units). ``xc`` must be monotonic; ``yc`` may increase or decrease. ``None`` if
    the window is empty."""
    minx, miny, maxx, maxy = bounds
    minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
    cols = np.nonzero((xc >= minx) & (xc <= maxx))[0]
    rows = np.nonzero((yc >= miny) & (yc <= maxy))[0]
    if cols.size == 0 or rows.size == 0:
        return None
    return slice(int(rows.min()), int(rows.max()) + 1), slice(int(cols.min()), int(cols.max()) + 1)


def h5_str(v: Any) -> str:
    """Decode an HDF5 scalar/bytes value to ``str``."""
    if isinstance(v, np.ndarray) and v.shape == ():
        v = v[()]
    if isinstance(v, (bytes, np.bytes_)):
        return v.decode("utf-8", "replace")
    return str(v)


def h5_str_list(v: Any) -> list[str]:
    arr = np.atleast_1d(np.asarray(v))
    return [h5_str(x) for x in arr]


def fill_to_nan(arr: np.ndarray, fill: Any = None) -> np.ndarray:
    """float32 copy with ``fill`` (if given and not NaN) set to NaN."""
    out = np.asarray(arr, dtype="float32").copy()
    if fill is not None:
        f = float(np.asarray(fill).ravel()[0]) if np.size(fill) else None
        if f is not None and not math.isnan(f):
            out[out == np.float32(f)] = np.nan
    return out
