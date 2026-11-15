# Connect the current React / ArcGIS frontend

The sibling frontend uses React + Vite with ArcGIS, `sharedMap` in `src/grid/powerGrid.ts`, 2D MapView and lazy 3D SceneView in `App.tsx`. It has existing power-line, forest mask and FIRMS/NIFC layers. This API supplies a **separate synthetic overlay**; do not feed its pixels into `tileMasks` or treat it as real forest/fire observations.

## 1. Start and discover

Start the backend using README. Add to frontend `.env.local`:

```text
VITE_BURNABILITY_API=http://127.0.0.1:8000
```

Restart Vite. Default CORS allows ports 5173 on localhost/127.0.0.1. For another frontend port set backend `CORS_ORIGINS` to its exact origin.

```ts
const api = import.meta.env.VITE_BURNABILITY_API ?? 'http://127.0.0.1:8000';
const resultUrl = `${api}/v1/burnability/results/forest-demo-v1`;
const response = await fetch(resultUrl);
if (!response.ok) throw new Error(`Backend: ${response.status}`);
const result = await response.json();
// Show SYNTHETIC DEMO and result.notices next to the legend.
```

No job call is necessary for the demo: it exists on startup. Keep the live FIRMS/NIFC controls separate from demo as_of and fictional arrows.

## 2. Raster overlay compatible with ArcGIS

Use `WebTileLayer`, not an ArcGIS TileLayer REST service URL. ArcGIS uses `{level}/{col}/{row}`, while XYZ uses `{z}/{x}/{y}`. They refer to the same ordering: col=x, row=y. Do not flip y (XYZ, not TMS).

```ts
import WebTileLayer from '@arcgis/core/layers/WebTileLayer.js';
import TileInfo from '@arcgis/core/layers/support/TileInfo.js';
import Extent from '@arcgis/core/geometry/Extent.js';
// In the existing frontend: import sharedMap, powerLinesLayer from './grid/powerGrid';

const definition = result.layers.find(
  (layer: { id: string }) => layer.id === 'susceptibility_index',
);
const [xmin, ymin, xmax, ymax] = result.bbox_wgs84;
const overlay = new WebTileLayer({
  id: 'burnability-demo',
  title: 'Fuel susceptibility · SYNTHETIC DEMO',
  urlTemplate: definition.arcgis_tiles_url,
  tileInfo: TileInfo.create({ numLODs: 19 }), // zoom levels 0..18
  fullExtent: new Extent({
    xmin, ymin, xmax, ymax, spatialReference: { wkid: 4326 },
  }),
  opacity: 0.65,
  // PNG nodata is already transparent.
});
// Keep the power lines above the colour overlay.
sharedMap.add(overlay, sharedMap.layers.indexOf(powerLinesLayer));
```

Add this once (e.g. a React effect with an async initialization guard). On effect cleanup remove and destroy the layer. Check an `active` flag after fetch so React StrictMode cleanup does not add stale duplicate layers. `sharedMap` is already used by 2D and 3D; do not add separate copies to each view. Show a loading/error state if backend startup fails.

Current map starts over the US near [-97,39]. Add a “California demo” action after `viewOnReady()`:

```ts
await view.goTo({ target: overlay.fullExtent, padding: 40 });
```

The overlay covers only the demo AOI. Outside it the backend returns transparent tiles. At higher zoom it enlarges the 200m source; show that resolution in the UI. No wheel-event fetch handler is needed: ArcGIS requests new tiles while the existing smooth-scroll code changes view zoom. Existing power-line detail thresholds (5.6, 7.2, 8.8) remain separate from raster resolution. Do not request gigantic images per scroll.

To switch layers, remove/destroy the old overlay and construct the selected layer with its own metadata URL and legend. All raster legends are fixed across zoom, and the index must not be labelled “probability”. Temperature is Celsius; LFMC is percent dry mass and can exceed 100%.

## 3. Vector overlay and zoom detail

The endpoint returns WGS84 GeoJSON LineStrings:

```ts
const extent = view.extent; // convert to geographic first if spatialReference is Web Mercator
const url = new URL(`${resultUrl}/vectors/illustrative_pathways`);
url.searchParams.set('bbox', '-120,37,-119.5,37.5'); // replace with current WGS84 viewport
url.searchParams.set('zoom', String(Math.max(0, Math.min(18, view.zoom))));
const geojson = await fetch(url, { signal: controller.signal }).then(r => r.json());
```

For a production interaction use `webMercatorUtils.webMercatorToGeographic(view.extent)` when needed; never send projected metres as lon/lat. Clamp to the API's supported geographic bounds and split antimeridian extents. 3D views can have changing/extreme footprints: use the geographic viewport envelope, and fall back to result bounds when it is unavailable.

Render returned geometry in a `GraphicsLayer` using `Polyline` with wkid=4326 and a line symbol with arrowhead at the **end**. Popups must say “Synthetic illustrative pathway; not forecast”. The bearing is clockwise from true north; coordinates already encode the correct line direction. For a simple first display a `GeoJSONLayer` pointing at the unfiltered endpoint can render all demo lines, but viewport filtering is preferred as regions grow.

Watch `view.stationary` with the frontend's existing `reactiveUtils`, debounce about 150–250ms and abort the previous fetch. Fetch when movement settles, not every mouse-wheel frame. Ignore stale responses after switching views/layers. Remove the watch and abort on cleanup. Reset pagination when bbox or zoom changes; follow `next_offset` only if needed. Keep at most the current viewport's graphics in the layer.

Backend vector detail matches existing frontend thresholds: below zoom 7.2 every 8th arrow; 7.2–8.8 every 4th; 8.8–10 every 2nd; 10+ all demo arrows. This thinning is display only. Arrow length is not speed or time; they are fictional west-to-east wind graphics, not algorithm forecast output.

## 4. Point values, images and downloads

On click convert `event.mapPoint` to WGS84 longitude/latitude and request:

```text
GET /v1/burnability/results/forest-demo-v1/point?lon=-119.75&lat=37.25
```

Display values using metadata units; null means unavailable, not zero. Response includes `sampled_cell_center` and `reason=OUTSIDE_AOI|NO_VALID_DATA` when invalid. Keep this popup separate from the existing power-line click behavior or add a dedicated inspection mode.

For cards/side panels use `preview_url + '?width=256'`, `512` or `1024`. The server supports 64..2048 and preserves dataset aspect ratio. A plain PNG preview is not a georeferenced map tile; use the tile endpoint for map overlays. Download button uses `download_url` for scientific GeoTIFF and vector layer `download_url` for GeoJSON. Metadata provides `provenance_url`.

## 5. Acceptance checks

- Zoom to the California demo and see the overlay in both 2D and 3D while retaining the existing power lines and fire layers.
- Scroll smoothly; only visible 256px tiles load and older requests do not replace newer views.
- Change layer, use its legend/units, and keep the SYNTHETIC DEMO notice visible.
- Outside coverage/lake pixels are transparent; point lookup returns null for missing data.
- Arrows thin at low zoom and show no invented percentage, travel time or speed.
- Backend errors do not break existing map layers. Real and synthetic results are clearly separated.

References: [ArcGIS WebTileLayer](https://developers.arcgis.com/javascript/latest/references/core/layers/WebTileLayer/), [FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/).

## Backend support for fuel-map screening

The backend exposes `GET /v1/burnability/results/forest-demo-v1/grid/susceptibility_index` for a future frontend integration. No frontend code changes are included.

The response contains EPSG:3857 `width,height,transform,values`, with rasterio affine `[a,b,c,d,e,f]`, row-major numeric values (null=nodata), scenario date, synthetic flag and exploratory threshold 0.25. It is a whole small-demo grid, not a general unlimited-area analysis service.

A frontend can retain its 3km proximity check to select candidate line spans, then sample this numeric grid to distinguish higher susceptibility, lower susceptibility and unknown. Sampling is:

```text
column = floor((mercator_x - transform[2]) / transform[0])
row    = floor((mercator_y - transform[5]) / transform[4])
value  = values[row * width + column]
```

Check bounds before indexing. Null/outside coverage means unknown, never low or safe. A value >= threshold is higher susceptibility under an exploratory policy; it is not a probability. Use short line subsegments relative to the source's approximately 200m support, rather than classifying a whole transmission line from a single point. Do not derive analytical values from coloured PNG tiles. Downloading the numeric grid once avoids thousands of point requests.

The frontend decides whether/how to display these classes. Its current power-line highlighting has not been changed. Candidate counts must remain labelled as proximity counts unless recalculated with documented classification semantics. A lower local fuel score cannot exclude fire reaching the line, spotting or damage.

The available grid is synthetic and its date is incompatible with live hotspot observations for scientific use. It supports integration development only. Future real results need compatible dates, validated maps and a versioned result endpoint. Weather-aware propagation and asset damage modelling remain separate work.

## Preferred integration: assess lines directly against the regional map

Use **`POST /v1/burnability/results/{result_id}/assess-lines`** to assess geometry from the displayed region. This method uses **no hotspot input and no 3km rule**. The result raster itself supplies the local assessment. Current available result is `forest-demo-v1`; its values are synthetic susceptibility, not an operational prediction. No frontend code has been modified.

Example request:

```json
{
  "region_bbox_wgs84": [-120,37,-119.5,37.5],
  "threshold": 0.25,
  "features": [
    {"type":"Feature","id":"power-line-123","geometry":{
      "type":"LineString","coordinates":[[-119.99,37.25],[-119.51,37.25]]
    }}
  ]
}
```

Convert rendered/query power-line geometry from Web Mercator to WGS84 before submitting. Use LineString or MultiLineString; preserve the source ID in Feature.id. Send up to 200 features per request and use bounded viewport batches. Clip lines to the displayed region first if summaries should describe only the visible portions: otherwise spans outside `region_bbox_wgs84` are returned as unknown and included in total submitted length. Dateline-crossing geometries should be split before submission. Cancel stale requests after viewport/selection changes; run on stationary views rather than each wheel event.

The response is a WGS84 GeoJSON FeatureCollection of short line spans. Each carries `source_index,source_id,classification,score,length_m,reason`. Render based on `classification=higher|lower|unknown`; numeric `score` is null for unknown. No-data/outside-region spans remain visible as unassessed, not safe. Returned `line_summaries` contain total, covered, higher, lower and unknown lengths in metres, coverage fraction, length-weighted mean score on covered spans, maximum sampled score and higher fraction of covered length. Classification is a threshold policy; the mean is not a probability of fire reaching a whole line. Never include unknown length in the denominator as if it were low susceptibility.

The backend samples native raster values at geodesic subsegment midpoints with at most 100m spacing, independent of map zoom. It returns approximate span boundaries, not an exact raster-vector polygon intersection. Raster support remains about 200m; neighbouring samples are not independent observations. Per-request budgets: 20,000 input vertices and 20,000 output samples, otherwise 422 requesting smaller batches.

This endpoint can highlight a susceptible span even when there is no hotspot within 3km. It describes local map conditions along the asset; it does not establish connected spread from a fire, time to impact, asset vulnerability or damage risk. When a genuinely predicted arrival/spread layer is available, add it as an explicitly named, versioned assessment layer with its event/window/units; do not relabel the current susceptibility score. A region-level prediction should drive regional screening; hotspot proximity can remain a separate contextual overlay.

## Showing a real model result

Backend branch `feat/real-results`, started with `RESULTS_DIR` (see `BACKEND.md`). The frontend has not been
changed; today it requests `forest-demo-v1` everywhere, so it keeps showing the demo. To show the newest real
result the changes are:

**`src/grid/burnability.ts`**

- `DEMO_RESULT_ID = 'forest-demo-v1'` → `'latest'`. Every endpoint accepts it; it falls back to the demo when
  the backend has no real result. Or read `latest` from `GET /v1/burnability/results`.
- `BurnabilityLayerId`: the fixed union → `string`. Real layer ids come from the metadata: `lfmc`, `lfmc_p05`,
  `lfmc_p95`, `soil_moisture`, `soil_moisture_change`, `lfmc_nisar_minus_smap`, `p_large_fire`, `risk_index`,
  `susceptibility_index`, `risk_class`, and whatever the model adds later.
- `createBurnabilityRasterLayer(layerId)`: take `urlTemplate` from the layer's `arcgis_tiles_url` and
  `fullExtent` from `metadata.bbox_wgs84` instead of `DEMO_BBOX_WGS84`; put "SYNTHETIC DEMO" in the title only
  when `metadata.synthetic` is true.
- `getDemoExtentMercator()`, and the `bbox` in `loadBurnabilityVectors`: use `metadata.bbox_wgs84`.
- `BurnabilityMetadata`: optional fields are available for the UI: `valid_from`, `valid_to`, `model_version`,
  `target`, `class_breaks`, `rank_reference`, `ordinary_day`, `nisar_share`, `notes`; each layer has
  `legend.stops`, `discrete` (true for `risk_class`: draw five swatches, not a ramp), `cyclic` (true for compass
  directions: label the ramp N E S W N, not 0–360) and `neutral` (1 for a ratio such as `weather_factor`: mark
  that value on the ramp and write values as "×2.4"; null otherwise).

**`src/App.tsx`**

- Map click (about line 369): the guard uses `DEMO_BBOX_WGS84`; use `burnMeta.bbox_wgs84`.
- Point card (about line 995): `pointSample.values.lfmc_pct` is the demo's id; a real result has
  `values.lfmc` (and `values.risk_index`, `values.risk_class`). `susceptibility_index` exists in both.
- `flyToBurnabilityDemo`: fly to `burnMeta.bbox_wgs84`.
- The default `burnLayerId` `'susceptibility_index'` exists in real results (a copy of `risk_index`), so the
  initial layer needs no change.

Nothing else is needed for the raster: tiles are transparent outside the result, at any zoom, and the colour
scale is fixed per layer. `vectors/illustrative_pathways` also answers for a real result (an alias of
`vectors`, the bundle's `vectors.geojson`).

**Arrows for the view.** When the metadata has `view_vectors_url`, ask
`${view_vectors_url}?bbox=<visible box>&n=1500&hours=1` once the map has come to rest and replace the arrows
with the answer: the bundle's file is too sparse to show at most zooms. A backend without the endpoint answers
404; fall back to `vectors/{id}` then. The frontend branch `feat/real-results` does this in
`src/grid/burnability.ts` (`loadBurnabilityVectors`).

**Fuel risk in the situation payload** (backend started with `FUEL_RISK_RESULT=latest`): optional fields to add
to the types in `src/grid/api.ts` and show where useful (fire card, report rows):
`Situation.fuelRisk?`, `FireCluster.fuelRiskIndex?` / `fuelRiskClass?`, `ThreatLine.fuelRisk?`
(`index`, `max`, `riskClass`, `highKm`, `coveredKm`), `sections[].fuelRiskIndex?`, and `spread[].fuelBoost?`
when `FUEL_RISK_DRIVES` contains `spread`. They are absent when the variable is unset, so existing rendering
is unaffected either way.

## Fire growth tool (`R/spread`)

Available when the result's metadata has `capabilities.spread` (a model result written by machine-learning
since commit `2704dec`); `spread_url` is the endpoint and `spread_limits` its ranges. The frontend branch
`feat/real-results` implements it as the "Fire growth" switch under the fuel-risk raster.

- Click → `GET {spread_url}?lon=&lat=&hours=1,3,6` (+ `&wind_speed=&wind_from=` for a wind typed by the user,
  `&fuel_age=false` to switch fuel age off). Use the concrete `result_id` from the metadata, not `latest`.
- Where the metadata has `capabilities.spread_fuel_state`, the same request with `&fuel_state=false` gives the
  fire on the fuel map alone, without NISAR's fuel state: ask both and draw the second as a dashed outline to
  show what the L-band fuel state changes (`properties.nisar_fuel_state` says which one a response is).
- Draw `features` (one `MultiPolygon` per hour, WGS84, longest first) as three outlines or translucent fills, and
  a marker at the click. `features[].properties`: `hours`, `area_ha`. A feature's `geometry` is null when nothing
  burned by then.
- Show from the top-level `properties`: `head_rate_m_min`, `head_direction_deg`, `flame_length_m`,
  `wind_10m_m_s`, `wind_from_deg`, `wind_source` (`map_day` | `request`), and say when `edge_reached` is true
  (the largest area is a lower bound).
- Errors carry a readable `detail`: 404 (this result has no fire-growth layers), 422 (outside the result, no
  burnable fuel near the point, parameters out of range). Show it; do not retry.
- Caption it truthfully: potential of a surface fire for one wind and the fuels of the map day, no
  suppression; not a forecast.
- Component layers (`spread_ros0`, …) are not in `layers`, so a layer picker built from the metadata does not
  list them; `component_layers` names them.

With `FUEL_RISK_DRIVES=simulate` the situation payload adds `spread[].simulated` (the same FeatureCollection, for
the live wind at each fire inside the result, else null) and `fuelRisk.simulated` (its description). Optional
to draw; absent without the flag.

