# fuel-lake — consumer SDK for processing microservices (direct lake access or the HTTP API)
# Licensed under Apache-2.0.
"""``LakeClient``: the one way processing services find and read lake data.

Two modes behind one interface, returning the same types:

- **direct**: a :class:`~fuellake.storage.Lake` on a local directory or an ``s3://`` root, e.g.
  ``LakeClient("/data/lake")``, ``LakeClient("s3://fuel-lake")``.
- **http**: the read-only API of :mod:`fuellake.services.api`, e.g. ``LakeClient(api_url="http://api:8000")``.

``LakeClient()`` / ``LakeClient.from_env()`` pick the mode from the environment: ``FUELLAKE_API_URL`` if set,
else the lake at ``LAKE_ROOT`` (plus ``FUELLAKE_S3_ENDPOINT_URL`` for MinIO).

Consumers never handle lake paths: they get STAC item dicts from :meth:`LakeClient.search` or
:meth:`LakeClient.get_item` and hand them back to :meth:`LakeClient.open_asset`.

=====================================  ==========================================================
``collections()``                      STAC collection dicts
``datasets()``                         dataset configs (status, licence, variables) as JSON dicts
``search(...)``                        STAC item dicts sorted by datetime; refuses mixed NISAR maturities
``get_item(collection, item_id)``      one item dict, or ``None``
``open_asset(item, key)``              ``xarray.DataArray``: form A COG (2-D ``y, x`` with CRS) or a
                                       variable of a form B Zarr item
``read_table(name, sources, start, end, dedupe)``  ``pandas.DataFrame`` (a ``GeoDataFrame`` for GeoParquet
                                       tables; ``"matchups"`` is the gold table)
``open_cube(aoi)``                     ``xarray.Dataset`` of the gold datacube
``events(since, types)``               event dicts, oldest first; ``since`` is inclusive
``publish_raster(dataset, aoi, ...)``  direct mode only: write a model output (form A COGs on the AOI
                                       grid) into an interface collection (``fuel-state``,
                                       ``spread-risk``); returns the cataloged item
``publish_table(dataset, aoi, df, key)``  direct mode only: the same for a GeoParquet table part
=====================================  ==========================================================

Publishing (:mod:`fuellake.publish`) validates against the dataset contract, clips to the lake region,
catalogs the item with ``derived_from`` links to its source items and emits events. HTTP mode is read-only and
raises :class:`ReadOnlyClientError` for the publish methods.

Reading strategy: rasters are read eagerly into memory (an AOI-sized COG is a few MB), so the arrays
keep no open file handles and are safe to use from any thread. Direct mode opens Zarr stores lazily
(dask); HTTP mode downloads a Zarr store to a temporary directory and loads it.
"""

from __future__ import annotations

import io
import os
import posixpath
import shutil
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fuellake import catalog, rasters, tables
from fuellake import events as _events
from fuellake.config import USER_AGENT, Settings
from fuellake.contracts import MixedMaturityError, check_single_maturity
from fuellake.storage import Lake

if TYPE_CHECKING:
    import pandas as pd
    import xarray as xr

API_URL_ENV = "FUELLAKE_API_URL"


class ReadOnlyClientError(RuntimeError):
    """A write (publish) was attempted through the read-only HTTP API."""

PAGE_SIZE = 1000          # items per HTTP search request (the API caps ``limit`` at 10000)

DateLike = date | datetime | str


def cube_rel(aoi: str) -> str:
    """Lake-relative path of the gold datacube (ARCHITECTURE.md lake layout)."""
    return f"features/{aoi}/cube.zarr"


class LakeClient:
    """Find and read lake data, directly or through the HTTP API (see the module docstring)."""

    def __init__(self, lake: Lake | str | os.PathLike | None = None, api_url: str | None = None, *,
                 timeout: float = 60.0):
        if lake is not None and api_url:
            raise ValueError("pass either a lake (direct mode) or api_url (HTTP mode), not both")
        if lake is None and not api_url:
            api_url = os.environ.get(API_URL_ENV) or None
            lake = None if api_url else Lake.from_env()
        if isinstance(lake, (str, os.PathLike)):
            root = str(lake)
            lake = Lake(root, storage_options=Settings.from_env().storage_options(root))
        self.lake: Lake | None = lake
        self.api_url: str | None = api_url.rstrip("/") if api_url else None
        self._http = None
        self._page_size = PAGE_SIZE
        if self.api_url:
            import httpx

            self._http = httpx.Client(base_url=self.api_url, timeout=timeout, follow_redirects=True,
                                      headers={"User-Agent": USER_AGENT})

    @classmethod
    def from_env(cls, api_url: str | None = None) -> LakeClient:
        """HTTP client on ``api_url`` or ``$FUELLAKE_API_URL``; otherwise direct on ``$LAKE_ROOT``."""
        api_url = api_url or os.environ.get(API_URL_ENV) or None
        return cls(api_url=api_url) if api_url else cls(lake=Lake.from_env())

    @property
    def mode(self) -> str:
        """``"http"`` or ``"direct"``."""
        return "http" if self._http is not None else "direct"

    def __repr__(self) -> str:
        return f"LakeClient(api_url={self.api_url!r})" if self.api_url else f"LakeClient({self.lake!r})"

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    def __enter__(self) -> LakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ discovery
    def collections(self) -> list[dict]:
        """All STAC collections of the catalog."""
        if self._http is not None:
            return self._get_json("/collections")["collections"]
        return catalog.list_collections(self._lake)

    def datasets(self) -> list[dict]:
        """Registered dataset configs (id, kind, status, licence, variables, caveats) as JSON dicts."""
        if self._http is not None:
            return self._get_json("/datasets")
        return dataset_dicts()

    def search(self, collections: list[str] | str | None = None, aoi: str | None = None,
               start: DateLike | None = None, end: DateLike | None = None,
               bbox: tuple[float, float, float, float] | None = None, query: dict[str, Any] | None = None,
               limit: int | None = None, allow_mixed_maturity: bool = False) -> list[dict]:
        """STAC items matching all filters, sorted by (datetime, id).

        ``collections``: ids (default all). ``aoi``: ``fuellake:aoi``. ``start``/``end``: dates, datetimes or
        ISO strings; the item interval must overlap and a bare ``end`` date includes that whole day.
        ``bbox``: WGS84 (west, south, east, north). ``query``: ``{"prop": value}`` or
        ``{"prop": {"eq"|"neq"|"lt"|"lte"|"gt"|"gte"|"in"|"contains": value}}``.

        Raises :class:`~fuellake.contracts.MixedMaturityError` if the items of one collection mix
        ``fuellake:maturity`` values (NISAR BETA/PROVISIONAL/VALIDATED), unless ``allow_mixed_maturity``.
        Narrow the search with ``query={"fuellake:maturity": "PROVISIONAL"}`` instead.
        """
        if isinstance(collections, str):
            collections = [collections]
        if self._http is not None:
            items = self._search_http(collections, aoi, start, end, bbox, query, limit)
        else:
            items = catalog.search(self._lake, collections, aoi=aoi, bbox=bbox, start=start, end=end,
                                   query=query, limit=limit)
        check_series_maturity(items, allow_mixed=allow_mixed_maturity)
        return items

    def get_item(self, collection: str, item_id: str) -> dict | None:
        """One STAC item (e.g. from an event's ``collection`` and ``item_id``), or ``None`` if absent."""
        if self._http is not None:
            try:
                return self._get_json(f"/collections/{_q(collection)}/items/{_q(item_id)}")
            except FileNotFoundError:
                return None
        return catalog.get_item(self._lake, collection, item_id)

    # ------------------------------------------------------------------ reading
    def open_asset(self, item: dict, key: str) -> xr.DataArray:
        """Asset ``key`` of ``item`` as an ``xarray.DataArray`` (loaded into memory for rasters).

        - Form A (COG): 2-D ``(y, x)`` on the AOI grid with CRS (``da.rio.crs``) and nodata
          (``da.rio.nodata``; NaN for continuous data). The asset key is the variable name.
        - Form B (Zarr): ``key`` is a variable of the Zarr asset (e.g. ``"t2m"``), or the Zarr asset key
          itself when the store holds a single variable. Dims ``(time, y, x)``.
        - Table assets raise ``TypeError``: use :meth:`read_table`.
        """
        assets = item.get("assets") or {}
        asset = assets.get(key)
        if asset is None:
            zarr_key = next((k for k, a in assets.items() if _asset_kind(a) == "zarr"), None)
            if zarr_key is None:
                raise KeyError(f"item {item.get('id')!r} has no asset {key!r}; assets: {sorted(assets)}")
            return self._zarr_variable(item, zarr_key, key)
        kind = _asset_kind(asset)
        if kind == "zarr":
            return self._zarr_variable(item, key, None)
        if kind == "table":
            raise TypeError(f"asset {key!r} is a table part; use LakeClient.read_table(...)")
        if kind != "raster":
            raise TypeError(f"asset {key!r} has unsupported media type {asset.get('type')!r}")
        da = _raster_from_bytes(self._asset_bytes(item, key))
        return _tag(da, item, key)

    def read_table(self, name: str, sources: list[str] | None = None, start: DateLike | None = None,
                   end: DateLike | None = None, dedupe: bool = True) -> pd.DataFrame:
        """Table ``name`` (``"lfmc_samples"``, ``"matchups"``, ``"fire_detections"``, ...) across all parts.

        ``sources`` keeps rows whose ``source`` column is in the list; ``start``/``end`` keep rows whose
        ``date`` is within ``[start, end]`` (inclusive days). ``dedupe`` (default) drops rows repeated across
        per-AOI parts on the table key (``sample_id`` for ``lfmc_samples``; see :mod:`fuellake.tables`).
        ``matchups`` holds the gold label x feature rows of every AOI that was built. Returns a
        ``geopandas.GeoDataFrame`` (EPSG:4326 ``geometry``) for GeoParquet tables, an empty ``DataFrame``
        when the table has no parts.
        """
        if self._http is not None:
            params = {"start": _day(start), "end": _day(end), "dedupe": None if dedupe else "false"}
            if sources is not None:
                params["sources"] = ",".join(sources)
            data = self._get_bytes(f"/tables/{_q(name)}", {k: v for k, v in params.items() if v is not None})
            return parquet_to_frame(data)
        return filter_table(tables.read_table(self._lake, name, dedupe=dedupe), sources, start, end)

    def open_cube(self, aoi: str) -> xr.Dataset:
        """The gold datacube ``features/<aoi>/cube.zarr`` (built by ``fuellake build-features``)."""
        if self._http is not None:
            return self._download_zarr(f"/cubes/{_q(aoi)}")
        rel = cube_rel(aoi)
        if not self._lake.exists(rel):
            raise FileNotFoundError(f"no datacube for AOI {aoi!r}; run `fuellake build-features --aoi {aoi}`")
        return rasters.open_zarr(self._lake, rel)

    def events(self, since: DateLike | None = None, types: Iterable[str] | None = None) -> list[dict]:
        """Events with ``time >= since`` (inclusive; all if ``None``), oldest first.

        De-duplicate on ``event_id`` (or on item id) when polling with the last seen time. Item events carry
        ``collection`` and ``item_id`` for :meth:`get_item`; ``href`` is lake-internal, do not rely on it.
        """
        types = list(types) if types else None
        if self._http is not None:
            params: dict[str, Any] = {}
            if since is not None:
                params["since"] = _iso(since)
            if types:
                params["type"] = types
            return self._get_json("/events", params)
        return _events.read_events(self._lake, since=since, types=types)

    # ------------------------------------------------------------------ publishing (direct mode)
    def publish_raster(self, dataset_id: str, aoi: str, datetime: DateLike, arrays: dict[str, Any], *,
                       properties: dict[str, Any] | None = None, source_items: list[dict | str] | None = None,
                       discriminator: str | None = None) -> dict:
        """Publish a model output into an interface collection (e.g. ``fuel-state``, ``spread-risk``).

        ``arrays`` maps variable name -> 2-D array on the AOI target grid (NumPy array of ``aoi.grid.shape`` or a
        georeferenced ``xarray.DataArray`` on that grid). ``properties`` holds the dataset's ``item_properties``
        (full or short names, e.g. ``"valid_from"``); ``source_items`` the input items (dicts from ``search`` or
        item ids), which become ``fuellake:source_items`` and ``derived_from`` links. Refuses a wrong grid, an
        unknown or missing required variable, a value outside ``valid_range`` and a missing or mistyped
        property (:class:`fuellake.publish.PublishError`). Returns the cataloged STAC item. Rules:
        :mod:`fuellake.publish`."""
        from fuellake.publish import publish_raster

        self._require_direct("publish_raster")
        return publish_raster(self._lake, dataset_id, aoi, datetime, arrays, properties=properties,
                              source_items=source_items, discriminator=discriminator)

    def publish_table(self, dataset_id: str, aoi: str, df: pd.DataFrame, key: str, *,
                      properties: dict[str, Any] | None = None, datetime: DateLike | None = None,
                      source_items: list[dict | str] | None = None) -> dict:
        """Publish a (Geo)DataFrame (``lon``/``lat`` or ``geometry``) as one GeoParquet part of an interface table
        collection. ``key`` identifies the part: publishing the same key again replaces it. Rows outside the
        lake region are dropped. Returns the cataloged STAC item (rules: :mod:`fuellake.publish`)."""
        from fuellake.publish import publish_table

        self._require_direct("publish_table")
        return publish_table(self._lake, dataset_id, aoi, df, key, properties=properties, datetime_=datetime,
                             source_items=source_items)

    def _require_direct(self, what: str) -> None:
        if self._http is not None:
            raise ReadOnlyClientError(
                f"{what}: this LakeClient reads through the HTTP API at {self.api_url}, which is read-only. "
                "Publishing needs direct mode: LakeClient('/path/to/lake') or LakeClient('s3://bucket/prefix') "
                f"(LakeClient.from_env() uses direct mode when {API_URL_ENV} is unset and LAKE_ROOT is set).")

    # ------------------------------------------------------------------ internals
    @property
    def _lake(self) -> Lake:
        assert self.lake is not None
        return self.lake

    def _request(self, path: str, params: dict | None = None, json: dict | None = None):
        assert self._http is not None
        r = self._http.post(path, json=json) if json is not None else self._http.get(path, params=params)
        if r.status_code == 404:
            raise FileNotFoundError(f"{self.api_url}{path}: {r.text[:200]}")
        r.raise_for_status()
        return r

    def _get_json(self, path: str, params: dict | None = None) -> Any:
        return self._request(path, params).json()

    def _get_bytes(self, path: str, params: dict | None = None) -> bytes:
        return self._request(path, params).content

    def _search_http(self, collections, aoi, start, end, bbox, query, limit) -> list[dict]:
        body = {"collections": list(collections) if collections is not None else None, "aoi": aoi,
                "bbox": list(bbox) if bbox is not None else None, "datetime": datetime_interval(start, end),
                "query": query}
        body = {k: v for k, v in body.items() if v is not None}
        out: list[dict] = []
        while limit is None or len(out) < limit:
            page = self._page_size if limit is None else min(self._page_size, limit - len(out))
            fc = self._request("/search", json={**body, "limit": page, "offset": len(out)}).json()
            out.extend(fc["features"])
            if not fc["features"] or not any(link.get("rel") == "next" for link in fc.get("links", [])):
                break
        return out

    def _asset_bytes(self, item: dict, key: str) -> bytes:
        if self._http is not None:
            return self._get_bytes(_asset_path(item, key))
        return self._lake.read_bytes(catalog.asset_rel(item, key))

    def _zarr_variable(self, item: dict, zarr_key: str, var: str | None) -> xr.DataArray:
        if self._http is not None:
            ds = self._download_zarr(_asset_path(item, zarr_key))
        else:
            ds = rasters.open_zarr(self._lake, catalog.asset_rel(item, zarr_key))
        names = [str(v) for v in ds.data_vars]
        if var is None:
            if len(names) != 1:
                raise KeyError(f"Zarr asset {zarr_key!r} holds variables {names}; pass one of them as key")
            var = names[0]
        if var not in ds.data_vars:
            raise KeyError(f"item {item.get('id')!r}: no asset or Zarr variable {var!r}; variables: {names}")
        return _tag(ds[var], item, var)

    def _download_zarr(self, path: str) -> xr.Dataset:
        import xarray as xr

        listing = self._get_json(path)
        tmp = tempfile.mkdtemp(prefix="fuellake-zarr-")
        try:
            for rel in listing["files"]:
                parts = rel.split("/")
                if rel.startswith("/") or ".." in parts:
                    raise ValueError(f"unsafe path in Zarr listing: {rel!r}")
                dst = os.path.join(tmp, *parts)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(self._get_bytes(f"{path}/{quote(rel)}"))
            ds = xr.open_zarr(tmp, consolidated=False, decode_coords="all")
            ds.load()
            ds.close()
            return ds
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------- helpers (shared with the API)
def check_series_maturity(items: Iterable[dict], allow_mixed: bool = False) -> None:
    """Raise :class:`MixedMaturityError` if the items of one collection mix ``fuellake:maturity`` values."""
    by_collection: dict[str, list[dict]] = {}
    for item in items:
        by_collection.setdefault(str(item.get("collection")), []).append(item)
    for cid, its in by_collection.items():
        try:
            check_single_maturity(its, allow_mixed=allow_mixed)
        except MixedMaturityError as exc:
            raise MixedMaturityError(
                f"collection {cid}: {exc}. Filter with query={{'fuellake:maturity': ...}} "
                "or pass allow_mixed_maturity=True") from None


def dataset_dicts() -> list[dict]:
    """Dataset configs as JSON-safe dicts; NaN nodata is encoded as ``"nan"`` (the STAC convention)."""
    from fuellake.registry import list_datasets

    out = []
    for ds in list_datasets():
        d = ds.model_dump(mode="json")
        for var, vd in zip(ds.variables, d.get("variables", []), strict=True):
            if var.nodata_is_nan:
                vd["nodata"] = "nan"
        out.append(d)
    return out


def filter_table(df: pd.DataFrame, sources: Iterable[str] | None = None, start: DateLike | None = None,
                 end: DateLike | None = None) -> pd.DataFrame:
    """Keep rows with ``source`` in ``sources`` and ``date`` in ``[start, end]`` (inclusive days)."""
    import pandas as pd

    if len(df) == 0:
        return df
    if sources is not None:
        if "source" not in df.columns:
            raise KeyError("table has no 'source' column to filter on")
        df = df[df["source"].isin(list(sources))]
    if start is not None or end is not None:
        if "date" not in df.columns:
            raise KeyError("table has no 'date' column to filter on")
        days = pd.to_datetime(df["date"]).dt.date        # Python dates, whatever the stored type
        keep = pd.Series(True, index=df.index)
        if start is not None:
            keep &= days >= _as_date(start)
        if end is not None:
            keep &= days <= _as_date(end)
        df = df[keep]
    return df.reset_index(drop=True)


def frame_to_parquet(df: pd.DataFrame) -> bytes:
    """Serialise a (Geo)DataFrame; GeoDataFrames become GeoParquet."""
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def parquet_to_frame(data: bytes) -> pd.DataFrame:
    """Inverse of :func:`frame_to_parquet`: a GeoDataFrame if the file carries GeoParquet metadata."""
    import geopandas as gpd
    import pandas as pd
    import pyarrow.parquet as pq

    meta = pq.read_schema(io.BytesIO(data)).metadata or {}
    return gpd.read_parquet(io.BytesIO(data)) if b"geo" in meta else pd.read_parquet(io.BytesIO(data))


def datetime_interval(start: DateLike | None, end: DateLike | None) -> str | None:
    """STAC ``datetime`` interval string (``"a/b"``, ``".."`` for open ends); bare dates stay bare."""
    if start is None and end is None:
        return None
    return f"{_iso(start) if start is not None else '..'}/{_iso(end) if end is not None else '..'}"


def _iso(v: DateLike) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        return catalog.iso(v)
    return v.isoformat()          # bare date: the server applies the same whole-day rule as direct mode


def _as_date(v: DateLike) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return catalog.to_utc(v).date()


def _day(v: DateLike | None) -> str | None:
    return None if v is None else _as_date(v).isoformat()


def _q(segment: str) -> str:
    return quote(str(segment), safe="")


def _asset_path(item: dict, key: str) -> str:
    return f"/assets/{_q(item['collection'])}/{_q(item['id'])}/{_q(key)}"


def _asset_kind(asset: dict) -> str:
    media = (asset.get("type") or "").lower()
    href = posixpath.basename(asset.get("href", "")).lower()
    if media == rasters.ZARR_MEDIA_TYPE or href.endswith(".zarr"):
        return "zarr"
    if media == tables.PARQUET_MEDIA_TYPE or href.endswith(".parquet"):
        return "table"
    if media.startswith("image/tiff") or href.endswith((".tif", ".tiff")):
        return "raster"
    return "other"


def _raster_from_bytes(data: bytes) -> xr.DataArray:
    import rioxarray
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as src:
        da = rioxarray.open_rasterio(src)
        da.load()
        da.close()        # release rioxarray's file manager now, not at interpreter shutdown
    if "band" in da.dims and da.sizes["band"] == 1:
        da = da.squeeze("band", drop=True)
    return da


def _tag(da: xr.DataArray, item: dict, name: str) -> xr.DataArray:
    da.name = name
    da.attrs["fuellake:item_id"] = item.get("id")
    da.attrs["fuellake:collection"] = item.get("collection")
    dt = (item.get("properties") or {}).get("datetime")
    if dt:
        da.attrs["datetime"] = dt
    return da
