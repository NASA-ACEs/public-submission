# fuel-lake — read-only HTTP API over the lake (STAC-API-lite; not a complete STAC API)
# Licensed under Apache-2.0.
"""Read-only HTTP API over the fuel-lake catalog. **This is STAC-API-lite, not a complete STAC API.**

Endpoints (all ``GET``; ``/search`` also ``POST``; asset and cube files also ``HEAD``):

==========================================  =============================================================
``/``                                       landing page (links, ``conformsTo``)
``/health``                                 ``{"status": "ok", "version": ..., "catalog": bool}``
``/datasets``                               dataset configs (status, licence, variables)
``/collections``, ``/collections/{cid}``    STAC collections
``/collections/{cid}/items``                items of one collection: ``bbox``, ``datetime``, ``limit``,
                                            ``offset``
``/collections/{cid}/items/{iid}``          one item
``/search``                                 ``collections``, ``ids``, ``bbox``, ``datetime``, ``query``
                                            (STAC query extension), ``aoi`` (``fuellake:aoi``), ``limit``,
                                            ``offset``
``/assets/{cid}/{iid}/{key}``               the asset file, streamed, with HTTP ``Range`` support (GDAL
                                            ``/vsicurl/`` and rasterio read COGs from it). For a Zarr
                                            asset: a JSON listing ``{"files": [...]}``; the files are at
                                            ``/assets/{cid}/{iid}/{key}/{path}``
``/cubes/{aoi}``, ``/cubes/{aoi}/{path}``   the gold datacube ``features/<aoi>/cube.zarr`` (listing, files)
``/tables/{name}``                          a table as one (Geo)Parquet file: ``sources``, ``start``, ``end``,
                                            ``dedupe`` (default true); ``/tables/matchups`` is the gold table
``/events``                                 events with ``time >= since`` (inclusive), ``type`` filter
==========================================  =============================================================

Item JSON returned here has absolute asset hrefs pointing at ``/assets/...`` and API links (``self``,
``parent``, ``collection``, ``root``), so any HTTP client can follow them.

Static collections (``fuellake:static: true``, e.g. cop-dem and landfire-fbfm40) ignore ``datetime``, as in
:func:`fuellake.catalog.search`: their items are time-invariant layers.

What is **not** implemented: ``intersects``, ``sortby``, ``fields``, ``filter`` (CQL2), transactions,
aggregation, token paging (paging uses ``offset`` in ``next`` links), free-text search. ``limit`` defaults
to 100 and is capped at 10000. The NISAR no-mixed-maturity guard lives in :class:`fuellake.client.LakeClient`
and the cube builder: raw API users must check ``fuellake:maturity`` themselves. Licences are not enforced:
asset responses carry ``X-Fuellake-License`` / ``X-Fuellake-Redistributable`` headers; do not expose the API
publicly while it serves non-redistributable data.

The lake is resolved per request (``Lake.from_env()``) unless :func:`create_app` is given one; listing
caches are dropped per request so a long-running API sees what the ETL writes. Credentials and storage
options are never returned.
"""

from __future__ import annotations

import copy
import json
import posixpath
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from fuellake import __version__, catalog
from fuellake.client import LakeClient, cube_rel, frame_to_parquet
from fuellake.rasters import ZARR_MEDIA_TYPE
from fuellake.storage import Lake
from fuellake.tables import PARQUET_MEDIA_TYPE

DEFAULT_LIMIT = 100
MAX_LIMIT = 10_000
CHUNK_SIZE = 1 << 20
GEOJSON = catalog.GEOJSON_TYPE
JSON_TYPE = catalog.JSON_TYPE
STRUCTURAL_RELS = {"self", "root", "parent", "child", "item", "items", "collection"}
CONFORMS_TO = [   # the classes whose basic requests work here; see the module docstring for the gaps
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/collections",
    "https://api.stacspec.org/v1.0.0/item-search",
    "https://api.stacspec.org/v1.0.0/item-search#query",
    "https://api.stacspec.org/v1.0.0/ogcapi-features",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
]
_SAFE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]+$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
DESCRIPTION = ("Read-only STAC-API-lite over the fuel-lake catalog. Not a complete STAC API implementation: "
               "no intersects, sortby, fields, CQL2 filter or transactions; paging by offset.")


class SearchBody(BaseModel):
    """``POST /search`` body (unknown fields are rejected with 422)."""

    model_config = ConfigDict(extra="forbid")
    collections: list[str] | None = None
    ids: list[str] | None = None
    bbox: list[float] | None = None
    datetime: str | None = None
    query: dict[str, Any] | None = None
    aoi: str | None = None
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(0, ge=0)


def create_app(lake: Lake | None = None) -> FastAPI:
    """Build the API app. ``lake`` pins the lake (tests); by default ``Lake.from_env()`` per request."""
    app = FastAPI(title="fuel-lake API", version=__version__, description=DESCRIPTION)

    def get_lake() -> Lake:
        lk = lake if lake is not None else Lake.from_env()
        lk.fs.invalidate_cache()            # the ETL writes behind our back: never trust listing caches
        return lk

    # ------------------------------------------------------------------ metadata
    @app.get("/")
    def landing(request: Request) -> dict:
        base = _base(request)
        return {
            "type": "Catalog", "stac_version": catalog.STAC_VERSION, "id": "fuel-lake-api",
            "title": "fuel-lake API", "description": DESCRIPTION, "conformsTo": CONFORMS_TO,
            "links": [
                {"rel": "self", "href": f"{base}/", "type": JSON_TYPE},
                {"rel": "root", "href": f"{base}/", "type": JSON_TYPE},
                {"rel": "data", "href": f"{base}/collections", "type": JSON_TYPE},
                {"rel": "search", "href": f"{base}/search", "type": GEOJSON, "method": "GET"},
                {"rel": "search", "href": f"{base}/search", "type": GEOJSON, "method": "POST"},
                {"rel": "service-desc", "href": f"{base}/openapi.json",
                 "type": "application/vnd.oai.openapi+json;version=3.0"},
                {"rel": "service-doc", "href": f"{base}/docs", "type": "text/html"},
            ],
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "catalog": get_lake().exists(catalog.CATALOG_REL)}

    @app.get("/datasets")
    def datasets() -> list[dict]:
        return LakeClient(get_lake()).datasets()

    @app.get("/collections")
    def collections(request: Request) -> dict:
        base = _base(request)
        cols = [_api_collection(c, base) for c in catalog.list_collections(get_lake())]
        return {"collections": cols, "links": [{"rel": "self", "href": f"{base}/collections", "type": JSON_TYPE},
                                               {"rel": "root", "href": f"{base}/", "type": JSON_TYPE}]}

    @app.get("/collections/{cid}")
    def collection(cid: str, request: Request) -> dict:
        return _api_collection(_collection_or_404(get_lake(), cid), _base(request))

    # ------------------------------------------------------------------ items and search
    @app.get("/collections/{cid}/items")
    def collection_items(request: Request, cid: str, bbox: str | None = None, datetime: str | None = None,
                         limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0)):
        lk = get_lake()
        _collection_or_404(lk, cid)
        params = dict(collections=[cid], bbox=_bbox(bbox), datetime=datetime)
        fc = _search(lk, request, params, limit, offset)
        fc["links"] += [{"rel": "collection", "href": f"{_base(request)}/collections/{_q(cid)}", "type": JSON_TYPE}]
        return JSONResponse(fc, media_type=GEOJSON)

    @app.get("/collections/{cid}/items/{iid}")
    def item(cid: str, iid: str, request: Request):
        it = catalog.get_item(get_lake(), _safe(cid), _safe(iid))
        if it is None:
            raise HTTPException(404, f"item {cid}/{iid} not found")
        return JSONResponse(_api_item(it, _base(request)), media_type=GEOJSON)

    @app.get("/search")
    def search_get(request: Request, collections: str | None = None, ids: str | None = None,
                   bbox: str | None = None, datetime: str | None = None, query: str | None = None,
                   aoi: str | None = None, limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                   offset: int = Query(0, ge=0)):
        try:
            q = json.loads(query) if query else None
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"query must be a JSON object: {exc}") from exc
        params = dict(collections=_csv(collections), ids=_csv(ids), bbox=_bbox(bbox), datetime=datetime,
                      query=q, aoi=aoi)
        return JSONResponse(_search(get_lake(), request, params, limit, offset), media_type=GEOJSON)

    @app.post("/search")
    def search_post(body: SearchBody, request: Request):
        params = body.model_dump(exclude={"limit", "offset"}, exclude_none=True)
        fc = _search(get_lake(), request, params, body.limit, body.offset, post_body=body.model_dump(exclude_none=True))
        return JSONResponse(fc, media_type=GEOJSON)

    # ------------------------------------------------------------------ files
    @app.api_route("/assets/{cid}/{iid}/{key}", methods=["GET", "HEAD"])
    def asset(cid: str, iid: str, key: str, request: Request):
        lk = get_lake()
        it, rel, a = _asset(lk, cid, iid, key)
        if a.get("type") == ZARR_MEDIA_TYPE:
            return _listing(lk, rel)
        return _send_file(lk, rel, request, a.get("type") or "application/octet-stream", _licence_headers(it))

    @app.api_route("/assets/{cid}/{iid}/{key}/{subpath:path}", methods=["GET", "HEAD"])
    def asset_file(cid: str, iid: str, key: str, subpath: str, request: Request):
        lk = get_lake()
        it, rel, a = _asset(lk, cid, iid, key)
        if a.get("type") != ZARR_MEDIA_TYPE:
            raise HTTPException(404, "only Zarr (directory) assets have sub-paths")
        return _send_file(lk, _inside(rel, subpath), request, "application/octet-stream", _licence_headers(it))

    @app.get("/cubes/{aoi}")
    def cube(aoi: str):
        return _listing(get_lake(), cube_rel(_safe(aoi)))

    @app.api_route("/cubes/{aoi}/{subpath:path}", methods=["GET", "HEAD"])
    def cube_file(aoi: str, subpath: str, request: Request):
        return _send_file(get_lake(), _inside(cube_rel(_safe(aoi)), subpath), request, "application/octet-stream")

    @app.get("/tables/{name}")
    def table(name: str, sources: str | None = None, start: str | None = None, end: str | None = None,
              dedupe: bool = True):
        try:
            df = LakeClient(get_lake()).read_table(_safe(name), sources=_csv(sources, keep_empty=True),
                                                    start=start, end=end, dedupe=dedupe)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return Response(frame_to_parquet(df), media_type=PARQUET_MEDIA_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{name}.parquet"'})

    @app.get("/events")
    def events(since: str | None = None, types: list[str] | None = Query(None, alias="type")) -> list[dict]:
        try:
            return LakeClient(get_lake()).events(since=since, types=types)
        except ValueError as exc:
            raise HTTPException(400, f"bad since: {exc}") from exc

    return app


# ---------------------------------------------------------------------------- request helpers
def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _q(segment: str) -> str:
    return quote(str(segment), safe="")


def _safe(segment: str) -> str:
    if not _SAFE.match(segment or ""):
        raise HTTPException(404, f"invalid identifier {segment!r}")
    return segment


def _csv(value: str | None, keep_empty: bool = False) -> list[str] | None:
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts if parts or keep_empty else None


def _bbox(value: str | None) -> list[float] | None:
    if not value:
        return None
    try:
        return [float(v) for v in value.split(",")]
    except ValueError as exc:
        raise HTTPException(400, f"bbox must be comma-separated numbers: {value!r}") from exc


def _collection_or_404(lake: Lake, cid: str) -> dict:
    coll = catalog.get_collection(lake, _safe(cid))
    if coll is None:
        raise HTTPException(404, f"collection {cid} not found")
    return coll


def _search(lake: Lake, request: Request, params: dict, limit: int, offset: int,
            post_body: dict | None = None) -> dict:
    """Run :func:`catalog.search` and wrap one page of results in a FeatureCollection with paging links."""
    bbox = params.get("bbox")
    if bbox is not None:
        if len(bbox) == 6:                  # 3-D bbox: drop the heights
            bbox = [bbox[0], bbox[1], bbox[3], bbox[4]]
        if len(bbox) != 4:
            raise HTTPException(400, "bbox needs 4 numbers: west,south,east,north")
    try:
        found = catalog.search(lake, params.get("collections"), aoi=params.get("aoi"), bbox=bbox,
                               datetime_range=params.get("datetime"), query=params.get("query"),
                               ids=params.get("ids"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    page = found[offset: offset + limit]
    base = _base(request)
    links: list[dict] = [{"rel": "root", "href": f"{base}/", "type": JSON_TYPE}]
    if offset + len(page) < len(found):
        nxt = offset + len(page)
        if post_body is None:
            links.append({"rel": "next", "href": str(request.url.include_query_params(offset=nxt)), "type": GEOJSON})
        else:
            links.append({"rel": "next", "href": f"{base}/search", "type": GEOJSON, "method": "POST",
                          "body": {**post_body, "offset": nxt}, "merge": False})
    return {"type": "FeatureCollection", "features": [_api_item(i, base) for i in page],
            "numberMatched": len(found), "numberReturned": len(page), "links": links}


def _api_item(item: dict, base: str) -> dict:
    """Item JSON for API clients: asset hrefs -> ``/assets/...`` URLs, links -> API URLs."""
    out = copy.deepcopy(item)
    cid, iid = out["collection"], out["id"]
    coll_url = f"{base}/collections/{_q(cid)}"
    for key, a in (out.get("assets") or {}).items():
        a["href"] = f"{base}/assets/{_q(cid)}/{_q(iid)}/{_q(key)}"
    keep = [lk for lk in out.get("links", []) if lk.get("rel") not in STRUCTURAL_RELS]
    out["links"] = [
        {"rel": "self", "href": f"{coll_url}/items/{_q(iid)}", "type": GEOJSON},
        {"rel": "parent", "href": coll_url, "type": JSON_TYPE},
        {"rel": "collection", "href": coll_url, "type": JSON_TYPE},
        {"rel": "root", "href": f"{base}/", "type": JSON_TYPE},
    ] + keep
    return out


def _api_collection(coll: dict, base: str) -> dict:
    """Collection JSON for API clients: static item links dropped, API links added."""
    out = copy.deepcopy(coll)
    url = f"{base}/collections/{_q(out['id'])}"
    keep = [lk for lk in out.get("links", []) if lk.get("rel") not in STRUCTURAL_RELS]
    out["links"] = [
        {"rel": "self", "href": url, "type": JSON_TYPE},
        {"rel": "root", "href": f"{base}/", "type": JSON_TYPE},
        {"rel": "parent", "href": f"{base}/", "type": JSON_TYPE},
        {"rel": "items", "href": f"{url}/items", "type": GEOJSON},
    ] + keep
    return out


def _asset(lake: Lake, cid: str, iid: str, key: str) -> tuple[dict, str, dict]:
    it = catalog.get_item(lake, _safe(cid), _safe(iid))
    if it is None or key not in (it.get("assets") or {}):
        raise HTTPException(404, f"asset {cid}/{iid}/{key} not found")
    try:
        rel = catalog.asset_rel(it, key)
    except ValueError as exc:
        raise HTTPException(404, f"asset {key} has no lake href") from exc
    return it, rel, it["assets"][key]


def _inside(root: str, subpath: str) -> str:
    rel = posixpath.normpath(f"{root}/{subpath}")
    if not rel.startswith(root + "/"):
        raise HTTPException(404, "path outside the store")
    return rel


def _listing(lake: Lake, rel: str) -> dict:
    files = [p[len(rel) + 1:] for p in lake.find(rel)]
    if not files:
        raise HTTPException(404, "store not found")
    return {"type": "directory", "media_type": ZARR_MEDIA_TYPE, "files": files}


def _licence_headers(item: dict) -> dict[str, str]:
    p = item.get("properties") or {}
    out = {}
    if p.get("fuellake:license") is not None:
        out["X-Fuellake-License"] = str(p["fuellake:license"])
    if p.get("fuellake:redistributable") is not None:
        out["X-Fuellake-Redistributable"] = "true" if p["fuellake:redistributable"] else "false"
    return out


# ---------------------------------------------------------------------------- byte ranges
def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``(first, last)`` byte of a single ``Range: bytes=`` header, ``None`` to send the whole file.

    Multi-range and malformed headers are ignored (RFC 9110 allows that). Raises ``ValueError`` when the
    range cannot be satisfied (-> 416).
    """
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m or (not m[1] and not m[2]):
        return None
    if not m[1]:                                    # suffix: last N bytes
        n = int(m[2])
        if n == 0 or size == 0:
            raise ValueError("unsatisfiable range")
        return max(0, size - n), size - 1
    first = int(m[1])
    last = int(m[2]) if m[2] else size - 1
    if first >= size or last < first:
        raise ValueError("unsatisfiable range")
    return first, min(last, size - 1)


def _send_file(lake: Lake, rel: str, request: Request, media_type: str, extra: dict | None = None) -> Response:
    """Stream one lake file (local or S3) with ``Range`` support; ``HEAD`` returns headers only."""
    try:
        info = lake.fs.info(lake.url(rel))
    except FileNotFoundError as exc:
        raise HTTPException(404, "file not found") from exc
    if info.get("type") != "file":
        raise HTTPException(404, "not a file")
    size = int(info["size"])
    headers = {"Accept-Ranges": "bytes", **(extra or {})}
    try:
        rng = parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    status, (first, last) = (200, (0, size - 1)) if rng is None else (206, rng)
    if rng is not None:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    length = max(0, last - first + 1)
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)
    return StreamingResponse(_iter_file(lake, rel, first, length), status_code=status, headers=headers,
                             media_type=media_type)


def _iter_file(lake: Lake, rel: str, first: int, length: int) -> Iterator[bytes]:
    with lake.open(rel, "rb") as f:
        f.seek(first)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


app = create_app()
