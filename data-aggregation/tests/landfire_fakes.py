# fuel-lake — offline fake of the LANDFIRE servers (LFPS ImageServers and landfire.gov files) for the LANDFIRE tests
# Licensed under Apache-2.0.
"""``FakeLandfire().get`` replaces the connectors' ``_get``: service JSON, ``exportImage`` clips (synthetic, per
service), plain files, and HTTP range requests into synthetic full-extent zips (206 + Content-Range)."""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np
from affine import Affine
from rasterio.io import MemoryFile

FIX = Path(__file__).parent / "fixtures"
CSV_FBFM40 = (FIX / "open_rasters" / "LF2025_FBFM40.csv").read_bytes()   # LF 2016 / 2022 CSVs are byte-identical
EVT_CSV = FIX / "landfire_evt"      # excerpts of the official LF2025_EVT.csv and LF16_EVT_200.csv (real rows)
# chaparral, coastal scrub, grass, developed, oak woodland, open water, a code only LF 2016 lists, an unlisted code
EVT_CODES = [7110, 7092, 9301, 7299, 7113, 7292, 7945, 7001]
LF_XML = FIX / "landfire"
# Facts shared by the live CONUS ImageServers (fetched 2026-11-14); LF 2016 and the topo services start one cell
# further west (-2362425), which is the same 30 m lattice.
EXTENT = {"xmin": -2362395, "ymin": 221265, "xmax": 2327655, "ymax": 3267405,
          "spatialReference": {"wkid": 5070, "latestWkid": 5070}}


class Resp:
    def __init__(self, content: bytes, ctype: str, status: int = 200, headers: dict | None = None):
        self.content, self.status_code = content, status
        self.headers = {"content-type": ctype, **(headers or {})}
        self.text = content.decode("utf-8", "replace")
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.content)

    def close(self):
        self.closed = True


def service_json(name: str) -> dict:
    lo, hi = (91, 204) if "FBFM40" in name else (-1, 4413)
    return {"name": name, "pixelType": "S16", "noDataValue": -9999, "pixelSizeX": 30, "pixelSizeY": 30,
            "capabilities": "Image,Metadata,Catalog,Mensuration", "extent": EXTENT,
            "spatialReference": {"wkid": 5070, "latestWkid": 5070}, "minValues": [lo], "maxValues": [hi]}


def fbfm40_codes(name: str, w: int, h: int) -> np.ndarray:
    """Stripes of real codes that differ per release, one unknown code (100) and a nodata block."""
    sets = {"LF2016": [142, 122, 102, 165, 98], "LF2022": [145, 121, 101, 165, 98],
            "LF2025": [147, 121, 102, 183, 98]}
    year = re.search(r"LF20\d\d", name).group(0)
    codes = np.array(sets.get(year, [102, 142, 165, 183, 98]), dtype="int16")
    arr = codes[(np.arange(w)[None, :].repeat(h, 0) // 10) % len(codes)]
    arr[h // 2 - 3:h // 2 + 3, w // 2 - 3:w // 2 + 3] = 100
    arr[-8:, :] = -9999
    return arr


def topo_codes(name: str, w: int, h: int) -> np.ndarray:
    """A plane rising 1 m per 30 m to the east (slope 1.9 deg, aspect 270) with a flat sea strip in the south."""
    cols = np.arange(w)[None, :].repeat(h, 0)
    if "Elev" in name:
        arr = (100 + cols).astype("int16")
        arr[-10:-4, :] = 0                           # coastal fill (sea at 0 m)
    elif "SlpD" in name:
        arr = np.full((h, w), 2, dtype="int16")
        arr[-10:-4, :] = 0
        arr[h // 2:h // 2 + 3, w // 4:w // 4 + 3] = 32767   # an implausible value: masked by valid_range
    else:                                            # Asp
        arr = np.full((h, w), 270, dtype="int16")
        arr[:, : w // 3] = -1                        # flat (slope <= 2 deg)
        arr[: h // 4, w // 3: 2 * w // 3] = 359
        arr[-10:-4, :] = -1                          # coastal fill
    arr[-4:, :] = -9999                              # beyond the coastal buffer
    return arr


def evt_codes(xmin: float, ymax: float, w: int, h: int) -> np.ndarray:
    """EVT codes in 300 m stripes fixed on the ground (two clips agree where they overlap), nodata in the south
    of every 3 km."""
    x = (np.round((xmin + 30 * np.arange(w)) / 30).astype("int64") // 10)[None, :].repeat(h, 0)
    y = (np.round((ymax - 30 * np.arange(h)) / 30).astype("int64") // 10)[:, None].repeat(w, 1)
    arr = np.array(EVT_CODES, dtype="int16")[(x + y // 3) % len(EVT_CODES)]
    arr[y % 10 == 0] = -9999
    return arr


def clip_bytes(name: str, params: dict) -> bytes:
    xmin, ymin, xmax, ymax = (float(v) for v in params["bbox"].split(","))
    w, h = (int(v) for v in params["size"].split(","))
    if "EVT" in name:
        arr = evt_codes(xmin, ymax, w, h)
    else:
        arr = fbfm40_codes(name, w, h) if "FBFM40" in name else topo_codes(name, w, h)
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=w, height=h, count=1, dtype="int16", crs="EPSG:5070",
                      transform=Affine(30, 0, xmin, 0, -30, ymax), nodata=-9999) as ds:
            ds.write(arr, 1)
        return mem.read()


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
        # stands for the multi-GB GeoTIFF member: stored (not deflated), larger than a few range-request buffers
        z.writestr("Tif/BIG_PLACEHOLDER.tif", b"\0" * 3_000_000, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def default_zips() -> dict[str, bytes]:
    lf16 = "LF2016_FBFM40_CONUS"
    lf22 = "LF2022_FBFM40_CONUS"
    return {
        f"https://landfire.gov/data-downloads/CONUS_LF2016/{lf16}.zip": zip_bytes({
            f"{lf16}/CSV_Data/LF2016_FBFM40.csv": CSV_FBFM40,
            f"{lf16}/General_Metadata/{lf16}.xml": (LF_XML / f"{lf16}.xml").read_bytes()}),
        f"https://landfire.gov/data-downloads/CONUS_LF2022/{lf22}.zip": zip_bytes({
            f"{lf22}/CSV_Data/LF2022_FBFM40.csv": CSV_FBFM40,
            f"{lf22}/General_Metadata/{lf22}.xml": (LF_XML / "LC22_F40_230.xml").read_bytes()}),
    }


class FakeLandfire:
    """Records every call in ``calls`` (url, params, headers) and the bytes served per zip in ``zip_bytes_served``."""

    def __init__(self, zips: dict[str, bytes] | None = None, honour_range: bool = True):
        self.zips = default_zips() if zips is None else zips
        self.honour_range = honour_range
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.zip_bytes_served: dict[str, int] = {}

    def get(self, connector, url, *, headers=None, stream=False, **params):
        self.calls.append((url, params, headers))
        if url.endswith("/exportImage"):
            name = url.split("/services/", 1)[1].removesuffix("/ImageServer/exportImage")
            return Resp(clip_bytes(name, params), "image/tiff")
        if url.endswith(".zip"):
            data = self.zips[url]
            rng = (headers or {}).get("Range")
            if not rng or not self.honour_range:
                return Resp(data, "application/zip")
            a, b = (int(v) for v in re.fullmatch(r"bytes=(\d+)-(\d+)", rng).groups())
            part = data[a:b + 1]
            self.zip_bytes_served[url] = self.zip_bytes_served.get(url, 0) + len(part)
            return Resp(part, "application/zip", 206, {"content-range": f"bytes {a}-{a + len(part) - 1}/{len(data)}"})
        if url.endswith(".csv"):
            local = EVT_CSV / url.rsplit("/", 1)[-1]
            return Resp(local.read_bytes() if local.is_file() else CSV_FBFM40, "text/csv")
        if url.endswith(".xml"):
            local = LF_XML / url.rsplit("/", 1)[-1]
            return Resp(local.read_bytes() if local.is_file() else b"<metadata/>", "application/xml")
        name = url.split("/services/", 1)[1].removesuffix("/ImageServer")
        return Resp(json.dumps(service_json(name)).encode(), "application/json")

    def patch(self, monkeypatch, *classes) -> FakeLandfire:
        """Replace ``_get`` of the connector classes by this fake (works for both ``_get`` signatures)."""
        def _get(conn, url, **kw):
            return self.get(conn, url, **kw)
        for cls in classes:
            monkeypatch.setattr(cls, "_get", _get)
        return self

    def calls_to(self, suffix: str) -> int:
        return sum(1 for url, _, _ in self.calls if url.endswith(suffix))
