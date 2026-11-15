"""HTTP routes for the transmission-grid wildfire risk API (``/v1/grid``)."""
import gzip
import hashlib
import json
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix='/v1/grid', tags=['grid'])
STALE_AFTER_S = 24 * 3600


def _service(request: Request):
    return request.app.state.grid


def _ready(request: Request):
    service = _service(request)
    if not service.store.ready:
        raise HTTPException(503, 'Grid cache is still syncing; retry shortly', headers={'Retry-After': '30'})
    return service


def _json(request: Request, payload, max_age: int):
    """Compact JSON with ETag and gzip; ``payload`` may already be encoded bytes."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(',', ':')).encode()
    etag = '"' + hashlib.sha1(body).hexdigest() + '"'
    headers = {'ETag': etag, 'Cache-Control': f'public, max-age={max_age}', 'Vary': 'Accept-Encoding'}
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    if 'gzip' in request.headers.get('accept-encoding', ''):
        body = gzip.compress(body, 5)
        headers['Content-Encoding'] = 'gzip'
    return Response(body, media_type='application/json', headers=headers)


@router.get('/status')
def status(request: Request):
    service = _service(request)
    meta_path = service.store.root / 'meta.json'
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    return dict(ready=service.store.ready, syncing=getattr(request.app.state, 'grid_syncing', False), meta=meta)


@router.get('/states')
def states(request: Request):
    """US states with line totals and operators, for the overview map and state picker."""
    return _json(request, _ready(request).store.states(), 3600)


@router.get('/states/{abbr}/lines')
def state_lines(abbr: str, request: Request):
    """All transmission lines of a state, each with its precomputed forest sub-paths (``fPaths``)."""
    grid = _ready(request).store.grid(abbr)
    if grid is None:
        raise HTTPException(404, 'Unknown state')
    return _json(request, grid.lines_json, 3600)


class SituationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hours: int = Field(72, ge=1, le=168)
    radius_m: int = Field(2000, ge=250, le=20000)
    owners: list[str] | None = None
    # Drop VIIRS detections outside forest (urban/industrial heat, farm burns).
    forest_only: bool = True


@router.post('/states/{abbr}/situation')
def situation(abbr: str, body: SituationRequest, request: Request):
    """Live fires, 24h spread envelopes, weather and the threatened line sections for one state.

    Everything the map needs, already classified and clipped; the client only renders it.
    """
    result = _ready(request).situation(abbr, body.hours, body.radius_m, body.owners, body.forest_only)
    if result is None:
        raise HTTPException(404, 'Unknown state')
    return _json(request, result, 60)


@router.get('/forest/tiles/{z}/{y}/{x}.png')
def forest_tile(z: int, y: int, x: int, request: Request):
    """USFS CONUS forest cover tiles, proxied and cached so the client talks only to this API."""
    if not 0 <= z <= 10 or not 0 <= x < 2 ** z or not 0 <= y < 2 ** z:
        raise HTTPException(422, 'Invalid tile (forest cover is published to z=10)')
    content = _service(request).forest_tile(z, y, x)
    if content is None:
        return Response(status_code=204, headers={'Cache-Control': 'public, max-age=86400'})
    return Response(content, media_type='image/png', headers={'Cache-Control': 'public, max-age=86400'})


def start_background_sync(app, sync_fn):
    """Sync on startup when the cache is missing or older than a day, then daily."""

    def stale():
        meta = app.state.grid.store.root / 'meta.json'
        if not meta.exists():
            return True
        try:
            return time.time() - json.loads(meta.read_text())['syncedAt'] / 1000 > STALE_AFTER_S
        except (ValueError, KeyError):
            return True

    def loop():
        while True:
            if stale():
                app.state.grid_syncing = True
                try:
                    sync_fn(app.state.grid.store.root)
                    app.state.grid.store.reload()
                    app.state.grid.reset()
                except Exception as error:  # keep serving the previous cache
                    print('[grid-sync] failed:', error, flush=True)
                finally:
                    app.state.grid_syncing = False
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True, name='grid-sync').start()
