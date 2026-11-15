# Burnability backend prototype

Branch: `prototype`. FastAPI backend serving deterministic **synthetic California forest** layers. Runs offline; no Earthdata/weather account needed. Includes OpenAPI, raster tiles, size-selectable PNG previews, GeoTIFF downloads, vector GeoJSON and point inspection.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/docs for interactive API docs. Discover layers at http://127.0.0.1:8000/v1/burnability/results/forest-demo-v1. Vite localhost/127.0.0.1:5173 origins are allowed by default. Add others with comma-separated `CORS_ORIGINS`; set `DATA_DIR` to change the generated output directory. Use one worker for this demo; data regenerate on startup. No authentication or remote job queue is implemented.

- [Frontend integration guide](docs/FRONTEND.md) — matched to the sibling React/ArcGIS frontend, including 2D/3D shared map, scroll zoom, vector filtering and click inspection.
- [Backend guide](docs/BACKEND.md) — endpoints, settings, limitations and real-data replacement.

```bash
python3 -m unittest discover -s tests -v
```

The sample covers approximately `[-120,37,-119.5,37.5]` (California foothills). Existing frontend starts over the entire US: zoom to this extent to see it. Synthetic fields imitate spatially continuous forest patterns; arrows depict fictional wind. They are not observations, model predictions or a fire forecast. Temperature is a display layer and is not used by the legacy susceptibility formula. The weather-aware v0.2 algorithm is still pending; this backend provides a usable display/API prototype now.

For **regional map-based power-line screening without the 3km rule**, use `POST /v1/burnability/results/forest-demo-v1/assess-lines`. Submit the viewport's line geometries; receive classified spans and length-weighted map summaries, including unknown coverage. See the preferred integration section in [docs/FRONTEND.md](docs/FRONTEND.md). The frontend repository is unchanged.

## Real model results (`RESULTS_DIR`)

```bash
RESULTS_DIR=/path/to/bundles uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Serves result bundles written by `machine-learning` (`python -m ml.map`) next to the demo, under their own
`result_id` and under the alias `latest`, with `synthetic: false`. `FUEL_RISK_RESULT=latest` adds the model's
fuel risk to the `/v1/grid` situation; `FUEL_RISK_DRIVES=spread,tiers` lets it scale the spread envelope and
feed the tiers, `simulate` adds the model's fire-growth simulation next to each envelope. All off by default.
A result with fire-growth layers also answers `GET /v1/burnability/results/{id}/spread?lon=&lat=&hours=1,3,6`:
where a fire started at that point would be after those hours, for the wind of the map day or a given wind.
`GET /v1/burnability/results/{id}/vectors?bbox=w,s,e,n&n=400` gives head-fire arrows sampled for a map view.
Details: [docs/BACKEND.md](docs/BACKEND.md), frontend changes needed: [docs/FRONTEND.md](docs/FRONTEND.md).

## Grid wildfire risk API (`/v1/grid`)

All analysis the map needs runs here; the frontend only renders the responses.

| Endpoint | What it returns |
|---|---|
| `GET /v1/grid/states` | US states: outline, line totals, km in forest, operators |
| `GET /v1/grid/states/{abbr}/lines` | Every transmission line of a state with its precomputed forest sub-paths (`fPaths`) |
| `POST /v1/grid/states/{abbr}/situation` | Body `{hours, radius_m, owners}`. Live VIIRS fires + NIFC perimeters, NWS weather, 24h spread envelopes, shut-off / PSPS tiers per line and the exact threatened line sections |
| `GET /v1/grid/forest/tiles/{z}/{y}/{x}.png` | USFS forest cover tiles, proxied and cached |
| `GET /v1/grid/status` | Cache readiness and last sync time |

Data cache lives in `data/grid` (`GRID_DATA_DIR` to change). It is built on startup when missing
or older than a day (~70 s from ArcGIS) and refreshed daily; until then grid endpoints answer `503`
with `Retry-After`. Build it manually with `python -m app.grid.sync`. Set `GRID_AUTO_SYNC=0` to
disable the background sync.

Fires are cached 10 min per state and window, weather 30 min, a computed situation 5 min; changing
only the buffer or operators reuses the fire/spread/exposure step.

## Deploying on Railway

`railway.json` sets the start command `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`
and a `/health` check; `.python-version` pins Python 3.12. Set `CORS_ORIGINS` to the frontend
origin. Attach a volume and point `GRID_DATA_DIR` at it to keep the grid cache across deploys.

**Real model results on Railway.** There is no file upload, so the backend can fetch the bundles itself:

1. Put the bundle archives (`.tar` or `.tar.gz`, bundle directories at the top level) at an address the server
   can reach. Where is the team's choice; the archives are not published anywhere by this repo.
2. Attach a volume and set `RESULTS_DIR=<volume>/results` and `RESULTS_CACHE_DIR=<volume>/results-cache`.
3. Set `RESULTS_ARCHIVE_URL=<address>[,<address>…]` (and `RESULTS_ARCHIVE_TOKEN` if the host wants a bearer
   token), and `FUEL_RISK_RESULT=latest` to use the model in `/v1/grid`.
4. Deploy. `/health` shows `results_archive: downloading`, then `ready` with `real_results` and `latest`.
   Restarts do not download again while the bundles are on the volume.

Space: the Santa Monica bundle is 52 MB, a statewide California bundle 500–630 MB. While an archive is being
unpacked it needs its own size twice, and overviews of large layers are cached as they are first viewed (up to
about the size of the bundle again): 0.3 GB for Santa Monica alone, 5 GB for all three bundles of v3.3.2.
Without a volume it works too, with a download at every start. Details: `docs/BACKEND.md`.
