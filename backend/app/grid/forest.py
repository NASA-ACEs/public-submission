"""USFS CONUS forest presence mask, sampled from the z=8 tiles of the FIA forest type service.

Tiles are downloaded once, reduced to boolean 256x256 alpha masks and cached on disk
(``forest_z8.npz``), so every later lookup is a vectorised numpy index.
"""
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from .geo import TWO_WORLD, WORLD_HALF, scale

TILES_URL = 'https://tiles.arcgis.com/tiles/gGHDlz6USftL5Pau/arcgis/rest/services/CONUSForestTypeGroups/MapServer'
Z = 8
N = 1 << Z
CONUS = (-14244268.17, 2608763.38, -7264863.68, 6737255.24)


class ForestMask:
    def __init__(self, tiles: dict[int, np.ndarray]):
        self.tiles = tiles

    def at(self, xs, ys) -> np.ndarray:
        xs = np.asarray(xs, dtype='float64')
        ys = np.asarray(ys, dtype='float64')
        out = np.zeros(xs.shape, dtype=bool)
        if xs.size == 0:
            return out
        tx = (xs + WORLD_HALF) / TWO_WORLD * N
        ty = (WORLD_HALF - ys) / TWO_WORLD * N
        col, row = tx.astype(int), ty.astype(int)
        px = np.clip(((tx - col) * 256).astype(int), 0, 255)
        py = np.clip(((ty - row) * 256).astype(int), 0, 255)
        keys = row * N + col
        for key in np.unique(keys):
            tile = self.tiles.get(int(key))
            if tile is None:
                continue
            sel = keys == key
            out[sel] = tile[py[sel], px[sel]]
        return out

    def fraction_around(self, mx, my, step_m=1200, radius=2):
        step = step_m / scale(my)
        offsets = np.arange(-radius, radius + 1) * step
        gx, gy = np.meshgrid(mx + offsets, my + offsets)
        return float(self.at(gx.ravel(), gy.ravel()).mean())


def _fetch_tile(client, row, col):
    try:
        res = client.get(f'{TILES_URL}/tile/{Z}/{row}/{col}')
        if res.status_code != 200 or not res.content:
            return None
        img = Image.open(io.BytesIO(res.content)).convert('RGBA')
        alpha = np.asarray(img)[:, :, 3] > 32
        return alpha if alpha.any() else None
    except (httpx.HTTPError, OSError, ValueError):
        return None


def download() -> ForestMask:
    xmin, ymin, xmax, ymax = CONUS
    c0, c1 = int((xmin + WORLD_HALF) / TWO_WORLD * N), int((xmax + WORLD_HALF) / TWO_WORLD * N)
    r0, r1 = int((WORLD_HALF - ymax) / TWO_WORLD * N), int((WORLD_HALF - ymin) / TWO_WORLD * N)
    jobs = [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
    tiles = {}
    with httpx.Client(timeout=30) as client, ThreadPoolExecutor(16) as pool:
        for (r, c), mask in zip(jobs, pool.map(lambda rc: _fetch_tile(client, *rc), jobs)):
            if mask is not None:
                tiles[r * N + c] = mask
    return ForestMask(tiles)


def save(mask: ForestMask, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **{str(k): np.packbits(v) for k, v in mask.tiles.items()})


def load(path: Path) -> ForestMask | None:
    if not path.exists():
        return None
    with np.load(path) as data:
        return ForestMask({int(k): np.unpackbits(data[k])[: 256 * 256].reshape(256, 256).astype(bool) for k in data.files})
