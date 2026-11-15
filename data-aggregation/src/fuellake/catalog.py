# fuel-lake — static, self-contained STAC catalog with incremental writes
# Licensed under Apache-2.0.
"""Static STAC catalog under ``catalog/`` (STAC 1.1, relative hrefs only, no ``self`` links).

Layout::

    catalog/catalog.json                          root catalog, one ``child`` link per collection
    catalog/<collection>/collection.json          collection, one ``item`` link per item
    catalog/<collection>/<item_id>/<item_id>.json item; asset hrefs are relative paths into ``staged/``

Writes are incremental: :func:`add_item` writes the item JSON and rewrites only its collection (to add
the item link and grow the extent). Other item files are never touched.

Items are plain ``dict`` objects (STAC JSON). Use :func:`asset_rel` to turn an asset href back into a
lake-relative path, and ``pystac.Item.from_dict`` if you want pystac objects.

The collection's ``item`` links are the index. :func:`search` and :func:`source_keys` read each item of
the selected collections (fine for hundreds of items; add an index if this becomes slow).

Static collections (``fuellake:static: true``: form A datasets whose ``cadence`` starts with ``static``, such as
``cop-dem`` dated 2021 and ``landfire-fbfm40`` dated 2025) hold time-invariant layers. :func:`search`
ignores ``start``/``end``/``datetime_range`` for them, so a search for a 2026 period still returns the DEM
and the fuel model. Pass ``static_any_time=False`` for strict STAC datetime semantics. Dated drops such as
``biomass`` (cadence ``on drop``) are not static here.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

from fuellake.storage import Lake

if TYPE_CHECKING:
    from fuellake.registry import DatasetConfig

STAC_VERSION = "1.1.0"
CATALOG_ID = "fuel-lake"
CATALOG_REL = "catalog/catalog.json"
JSON_TYPE = "application/json"
GEOJSON_TYPE = "application/geo+json"

PROJ_EXT = "https://stac-extensions.github.io/projection/v1.1.0/schema.json"   # defines proj:epsg
RASTER_EXT = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
SAT_EXT = "https://stac-extensions.github.io/sat/v1.0.0/schema.json"
SAR_EXT = "https://stac-extensions.github.io/sar/v1.0.0/schema.json"

_SPDX_IDS = {"CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0", "CC-BY-NC-4.0", "Apache-2.0", "MIT", "ODbL-1.0",
             "PDDL-1.0", "ODC-By-1.0", "CDLA-Permissive-2.0"}


# ---------------------------------------------------------------------------- paths and hrefs
def collection_rel(collection_id: str) -> str:
    return f"catalog/{collection_id}/collection.json"


def item_rel(collection_id: str, item_id: str) -> str:
    return f"catalog/{collection_id}/{item_id}/{item_id}.json"


def relative_href(from_rel: str, to_rel: str) -> str:
    """Relative href from the JSON file ``from_rel`` to the lake path ``to_rel``."""
    if "://" in to_rel or to_rel.startswith("/"):
        raise ValueError(f"expected a lake-relative path, got {to_rel!r}")
    href = posixpath.relpath(to_rel, posixpath.dirname(from_rel) or ".")
    return href if href.startswith("../") else f"./{href}"


def resolve_href(from_rel: str, href: str) -> str:
    """Lake-relative path of ``href`` written in the JSON file ``from_rel``."""
    if "://" in href or href.startswith("/"):
        raise ValueError(f"absolute href not allowed in this catalog: {href!r}")
    return posixpath.normpath(posixpath.join(posixpath.dirname(from_rel), href))


def asset_rel(item: dict, key: str) -> str:
    """Lake-relative path of asset ``key`` of an item dict."""
    return resolve_href(item_rel(item["collection"], item["id"]), item["assets"][key]["href"])


# ---------------------------------------------------------------------------- datetimes
def to_utc(value: datetime | date | str, *, end_of_day: bool = False) -> datetime:
    """Parse/normalise to an aware UTC datetime. A bare date is 00:00 (or 23:59:59.999999 if end_of_day)."""
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        value = datetime.fromisoformat(s) if ("T" in s or " " in s or len(s) > 10) else date.fromisoformat(s)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=UTC)


def iso(dt: datetime | date | str) -> str:
    return to_utc(dt).isoformat().replace("+00:00", "Z")


def parse_datetime_interval(value: str | None) -> tuple[datetime | None, datetime | None]:
    """STAC API ``datetime`` parameter: ``"2026-06-01"``, ``"a/b"``, ``"../b"``, ``"a/.."``."""
    if not value:
        return None, None
    if "/" not in value:
        return to_utc(value), to_utc(value, end_of_day=True)
    a, b = value.split("/", 1)
    start = None if a in ("", "..") else to_utc(a)
    end = None if b in ("", "..") else to_utc(b, end_of_day=True)
    return start, end


def item_interval(item: dict) -> tuple[datetime, datetime]:
    p = item["properties"]
    start = p.get("start_datetime") or p.get("datetime")
    end = p.get("end_datetime") or p.get("datetime")
    return to_utc(start), to_utc(end)


# ---------------------------------------------------------------------------- catalog and collections
def init_catalog(lake: Lake, title: str = "fuel-lake",
                 description: str = "fuel-lake data catalog (soil moisture -> LFMC -> fire spread risk)") -> dict:
    """Create ``catalog/catalog.json`` if missing; return the catalog dict."""
    if lake.exists(CATALOG_REL):
        return lake.read_json(CATALOG_REL)
    cat = {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": CATALOG_ID,
        "title": title,
        "description": description,
        "links": [{"rel": "root", "href": "./catalog.json", "type": JSON_TYPE, "title": title}],
    }
    lake.write_json(CATALOG_REL, cat)
    return cat


def _ensure_child_link(lake: Lake, collection_id: str, title: str) -> None:
    cat = init_catalog(lake)
    href = relative_href(CATALOG_REL, collection_rel(collection_id))
    if any(link.get("rel") == "child" and link.get("href") == href for link in cat["links"]):
        return
    cat["links"].append({"rel": "child", "href": href, "type": JSON_TYPE, "title": title})
    lake.write_json(CATALOG_REL, cat)


def get_collection(lake: Lake, collection_id: str) -> dict | None:
    rel = collection_rel(collection_id)
    return lake.read_json(rel) if lake.exists(rel) else None


def list_collections(lake: Lake) -> list[dict]:
    """Collections linked from the root catalog."""
    if not lake.exists(CATALOG_REL):
        return []
    out = []
    for link in lake.read_json(CATALOG_REL)["links"]:
        if link.get("rel") == "child":
            rel = resolve_href(CATALOG_REL, link["href"])
            if lake.exists(rel):
                out.append(lake.read_json(rel))
    return out


def upsert_collection(lake: Lake, dataset: DatasetConfig, bbox: Iterable[float] | None = None) -> dict:
    """Create or refresh the collection for ``dataset`` (collection id = dataset id).

    Metadata (title, licence, status, variables, caveats) is refreshed from the config; existing item
    links and extent are preserved. ``bbox`` seeds the spatial extent of a new, empty collection.
    """
    rel = collection_rel(dataset.id)
    old = get_collection(lake, dataset.id) or {}
    item_links = [link for link in old.get("links", []) if link.get("rel") == "item"]
    lic = dataset.license
    links: list[dict[str, Any]] = [
        {"rel": "root", "href": "../catalog.json", "type": JSON_TYPE},
        {"rel": "parent", "href": "../catalog.json", "type": JSON_TYPE},
    ]
    if lic.url:
        links.append({"rel": "license", "href": lic.url, "title": lic.id})
    if dataset.homepage:
        links.append({"rel": "about", "href": dataset.homepage, "title": "Source homepage"})
    description = dataset.title
    if dataset.provider:
        description += f". Provider: {dataset.provider}."
    if dataset.caveats:
        description += " Caveats: " + " ".join(dataset.caveats)
    extent = old.get("extent") or {
        "spatial": {"bbox": [list(bbox) if bbox is not None else [-180.0, -90.0, 180.0, 90.0]]},
        "temporal": {"interval": [[None, None]]},
    }
    coll = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "id": dataset.id,
        "title": dataset.title,
        "description": description,
        "license": lic.id if lic.id in _SPDX_IDS else "other",
        "providers": [{"name": dataset.provider, "roles": ["producer"], "url": dataset.homepage}]
        if dataset.provider else [],
        "extent": extent,
        "keywords": ["fuel-lake", dataset.kind],
        "fuellake:kind": dataset.kind,
        "fuellake:static": dataset.is_static,
        "fuellake:table": dataset.table,
        "fuellake:status": dataset.status,
        "fuellake:license": lic.model_dump(),
        "fuellake:redistributable": lic.redistributable,
        "fuellake:maturity": dataset.maturity,
        "fuellake:variables": [_jsonable_variable(v) for v in dataset.variables],
        "fuellake:caveats": dataset.caveats,
        "links": links + item_links,
    }
    lake.write_json(rel, coll)
    _ensure_child_link(lake, dataset.id, dataset.title)
    return coll


def _jsonable_variable(v) -> dict:
    d = v.model_dump()
    if v.nodata_is_nan:
        d["nodata"] = "nan"
    return d


# ---------------------------------------------------------------------------- items
def make_item(collection_id: str, item_id: str, dt: datetime | date | str, properties: dict,
              assets: dict[str, dict], bbox: Iterable[float], geometry: dict | None = None, *,
              start: datetime | date | str | None = None, end: datetime | date | str | None = None,
              stac_extensions: Iterable[str] = ()) -> dict:
    """Build a STAC item dict.

    ``assets`` maps key -> ``{"path": <lake-relative path>, "type": ..., "roles": [...], **extra}``;
    ``path`` becomes an href relative to the item JSON. ``bbox`` is WGS84 (west, south, east, north);
    ``geometry`` defaults to the bbox polygon.
    """
    w, s, e, n = (float(v) for v in bbox)
    rel = item_rel(collection_id, item_id)
    props = dict(properties)
    props["datetime"] = iso(dt)
    if start is not None:
        props["start_datetime"] = iso(start)
    if end is not None:
        props["end_datetime"] = iso(end)
    out_assets = {}
    for key, spec in assets.items():
        spec = dict(spec)
        path = spec.pop("path", None)
        if path is not None:
            spec["href"] = relative_href(rel, path)
        out_assets[key] = spec
    return {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": sorted(set(stac_extensions)),
        "id": item_id,
        "collection": collection_id,
        "geometry": geometry or {
            "type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
        },
        "bbox": [w, s, e, n],
        "properties": props,
        "assets": out_assets,
        "links": [
            {"rel": "root", "href": "../../catalog.json", "type": JSON_TYPE},
            {"rel": "parent", "href": "../collection.json", "type": JSON_TYPE},
            {"rel": "collection", "href": "../collection.json", "type": JSON_TYPE},
        ],
    }


def add_item(lake: Lake, item: dict) -> tuple[str, bool]:
    """Write (create or replace) an item and link it from its collection.

    Returns ``(item lake-relative path, created)``; ``created`` is False when an item with the same id
    already existed. The collection must exist (see :func:`upsert_collection`).
    """
    cid, iid = item["collection"], item["id"]
    crel = collection_rel(cid)
    coll = get_collection(lake, cid)
    if coll is None:
        raise KeyError(f"collection {cid!r} does not exist; call upsert_collection first")
    rel = item_rel(cid, iid)
    created = not lake.exists(rel)
    lake.write_json(rel, item)

    href = relative_href(crel, rel)
    links = coll["links"]
    had_items = any(link.get("rel") == "item" for link in links)
    if not any(link.get("rel") == "item" and link.get("href") == href for link in links):
        links.append({"rel": "item", "href": href, "type": GEOJSON_TYPE})
    _grow_extent(coll, item, first=not had_items)
    lake.write_json(crel, coll)
    return rel, created


def _grow_extent(coll: dict, item: dict, first: bool) -> None:
    w, s, e, n = item["bbox"]
    start, end = item_interval(item)
    spatial = coll["extent"]["spatial"]["bbox"][0]
    if first:
        coll["extent"]["spatial"]["bbox"][0] = [w, s, e, n]
        coll["extent"]["temporal"]["interval"][0] = [iso(start), iso(end)]
        return
    coll["extent"]["spatial"]["bbox"][0] = [min(spatial[0], w), min(spatial[1], s),
                                            max(spatial[2], e), max(spatial[3], n)]
    t0, t1 = coll["extent"]["temporal"]["interval"][0]
    t0 = iso(start) if t0 is None or to_utc(t0) > start else t0
    t1 = iso(end) if t1 is None or to_utc(t1) < end else t1
    coll["extent"]["temporal"]["interval"][0] = [t0, t1]


def get_item(lake: Lake, collection_id: str, item_id: str) -> dict | None:
    rel = item_rel(collection_id, item_id)
    return lake.read_json(rel) if lake.exists(rel) else None


def remove_item(lake: Lake, collection_id: str, item_id: str) -> bool:
    """Unlink and delete an item JSON (staged files are left alone). Returns True if it existed."""
    coll = get_collection(lake, collection_id)
    rel = item_rel(collection_id, item_id)
    existed = lake.exists(rel)
    if coll is not None:
        href = relative_href(collection_rel(collection_id), rel)
        coll["links"] = [lk for lk in coll["links"] if not (lk.get("rel") == "item" and lk.get("href") == href)]
        lake.write_json(collection_rel(collection_id), coll)
    if existed:
        lake.rm(posixpath.dirname(rel), recursive=True)
    return existed


def item_ids(lake: Lake, collection_id: str) -> list[str]:
    coll = get_collection(lake, collection_id)
    if coll is None:
        return []
    crel = collection_rel(collection_id)
    ids = []
    for link in coll["links"]:
        if link.get("rel") == "item":
            ids.append(posixpath.basename(resolve_href(crel, link["href"]))[: -len(".json")])
    return ids


def iter_items(lake: Lake, collection_id: str) -> Iterator[dict]:
    for iid in item_ids(lake, collection_id):
        item = get_item(lake, collection_id, iid)
        if item is not None:
            yield item


def source_keys(lake: Lake, collection_id: str, aoi: str | None = None) -> dict[str, list[str]]:
    """Map ``fuellake:source_key`` -> item ids for one collection, optionally only items of ``aoi``
    (used for idempotent runs: the same granule is processed once per AOI)."""
    out: dict[str, list[str]] = {}
    for item in iter_items(lake, collection_id):
        if aoi is not None and item["properties"].get("fuellake:aoi") != aoi:
            continue
        key = item["properties"].get("fuellake:source_key")
        if key is not None:
            out.setdefault(str(key), []).append(item["id"])
    return out


def find_by_source_key(lake: Lake, collection_id: str, key: str) -> list[dict]:
    return [i for i in iter_items(lake, collection_id) if i["properties"].get("fuellake:source_key") == key]


# ---------------------------------------------------------------------------- search
def _bbox_intersects(a: Iterable[float], b: Iterable[float]) -> bool:
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return not (ae < bw or be < aw or an < bs or bn < as_)


QUERY_OPS = ("eq", "neq", "lt", "lte", "gt", "gte", "in", "contains")


def _match(value: Any, cond: Any) -> bool:
    if not isinstance(cond, dict):
        return value == cond
    for op, ref in cond.items():
        if op not in QUERY_OPS:
            raise ValueError(f"unknown query operator {op!r}; use one of {QUERY_OPS}")
        if op == "eq" and not value == ref:
            return False
        if op == "neq" and not value != ref:
            return False
        if op in ("lt", "lte", "gt", "gte"):
            if value is None:
                return False
            if op == "lt" and not value < ref:
                return False
            if op == "lte" and not value <= ref:
                return False
            if op == "gt" and not value > ref:
                return False
            if op == "gte" and not value >= ref:
                return False
        if op == "in" and value not in ref:
            return False
        if op == "contains" and (not isinstance(value, (list, tuple)) or ref not in value):
            return False
    return True


def search(lake: Lake, collections: Iterable[str] | None = None, *, aoi: str | None = None,
           bbox: Iterable[float] | None = None, start: datetime | date | str | None = None,
           end: datetime | date | str | None = None, datetime_range: str | None = None,
           query: dict | None = None, ids: Iterable[str] | None = None, limit: int | None = None,
           static_any_time: bool = True) -> list[dict]:
    """Filter items. All filters are ANDed; results sorted by (datetime, id).

    - ``collections``: ids (default all); ``aoi``: ``fuellake:aoi`` value.
    - ``bbox``: WGS84 intersection with the item bbox.
    - ``start``/``end`` (date, datetime or ISO string; a bare ``end`` date includes that whole day) or
      ``datetime_range`` (STAC ``"a/b"`` syntax): the item interval must overlap. Not applied to static
      collections (``fuellake:static``) unless ``static_any_time=False`` (see the module docstring).
    - ``query``: ``{"prop": value}`` (equality) or ``{"prop": {"eq"|"neq"|"lt"|"lte"|"gt"|"gte"|"in"|"contains": v}}``.
    """
    cids = list(collections) if collections is not None else [c["id"] for c in list_collections(lake)]
    t0 = to_utc(start) if start is not None else None
    t1 = to_utc(end, end_of_day=True) if end is not None else None
    if datetime_range:
        r0, r1 = parse_datetime_interval(datetime_range)
        t0 = r0 if r0 is not None else t0
        t1 = r1 if r1 is not None else t1
    id_set = set(ids) if ids is not None else None
    bbox = list(bbox) if bbox is not None else None
    out = []
    for cid in cids:
        timed = t0 is not None or t1 is not None
        if timed and static_any_time and (get_collection(lake, cid) or {}).get("fuellake:static"):
            timed = False                      # time-invariant layer: valid for any period
        for item in iter_items(lake, cid):
            props = item["properties"]
            if id_set is not None and item["id"] not in id_set:
                continue
            if aoi is not None and props.get("fuellake:aoi") != aoi:
                continue
            if bbox is not None and not _bbox_intersects(item["bbox"], bbox):
                continue
            if timed:
                i0, i1 = item_interval(item)
                if (t1 is not None and i0 > t1) or (t0 is not None and i1 < t0):
                    continue
            if query and not all(_match(props.get(k), c) for k, c in query.items()):
                continue
            out.append(item)
    out.sort(key=lambda it: (item_interval(it)[0], it["id"]))
    return out[:limit] if limit is not None else out
