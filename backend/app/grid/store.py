"""In-memory access to the synced grid cache (see ``sync.py``)."""
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import forest as forest_mod

GRID_DIR = Path(os.getenv('GRID_DATA_DIR', Path(__file__).resolve().parents[2] / 'data' / 'grid')).resolve()


class StateGrid:
    """Lines of one state plus vectorised bounding boxes for fast spatial prefilters."""

    def __init__(self, summary, lines):
        self.summary = summary
        self.lines = lines
        self.bboxes = np.array([_bbox(l['paths']) for l in lines], dtype='float64').reshape(-1, 4)
        self.first_points = np.array([l['paths'][0][0] for l in lines], dtype='float64').reshape(-1, 2)
        self.lines_json = json.dumps(dict(abbr=summary['abbr'], lines=[
            {k: l[k] for k in ('oid', 'owner', 'kv', 'sub1', 'sub2', 'km', 'fKm', 'paths', 'fPaths')} for l in lines
        ]), separators=(',', ':')).encode()


def _bbox(paths):
    xs = [p[0] for path in paths for p in path]
    ys = [p[1] for path in paths for p in path]
    return min(xs), min(ys), max(xs), max(ys)


class GridStore:
    def __init__(self, root: Path = GRID_DIR, max_states=12):
        self.root = root
        self.max_states = max_states
        self._lock = threading.Lock()
        self._states = None
        self._by_abbr = {}
        self._grids = OrderedDict()
        self._forest = None

    @property
    def ready(self):
        return (self.root / 'states.json').exists()

    def reload(self):
        with self._lock:
            self._states = None
            self._grids.clear()
            self._forest = None

    def states(self):
        with self._lock:
            if self._states is None:
                self._states = json.loads((self.root / 'states.json').read_text())
                self._by_abbr = {s['abbr']: s for s in self._states}
            return self._states

    def state(self, abbr):
        self.states()
        return self._by_abbr.get(abbr.upper())

    def grid(self, abbr) -> StateGrid | None:
        summary = self.state(abbr)
        if summary is None:
            return None
        with self._lock:
            if summary['abbr'] in self._grids:
                self._grids.move_to_end(summary['abbr'])
                return self._grids[summary['abbr']]
        path = self.root / 'states' / f"{summary['abbr']}.json"
        lines = json.loads(path.read_text())['lines'] if path.exists() else []
        grid = StateGrid(summary, lines)
        with self._lock:
            self._grids[summary['abbr']] = grid
            while len(self._grids) > self.max_states:
                self._grids.popitem(last=False)
        return grid

    def forest(self) -> forest_mod.ForestMask:
        with self._lock:
            if self._forest is None:
                self._forest = forest_mod.load(self.root / 'forest_z8.npz') or forest_mod.ForestMask({})
            return self._forest
