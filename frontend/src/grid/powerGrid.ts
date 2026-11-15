import Ground from '@arcgis/core/Ground';
import ArcGISMap from '@arcgis/core/Map.js';
import Color from '@arcgis/core/Color.js';
import Basemap from '@arcgis/core/Basemap.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import type Layer from '@arcgis/core/layers/Layer.js';
import TileLayer from '@arcgis/core/layers/TileLayer.js';
import WebTileLayer from '@arcgis/core/layers/WebTileLayer.js';
import TileInfo from '@arcgis/core/layers/support/TileInfo.js';
import { forestTilesTemplate } from './api';
import { type BasemapMode } from './geo';

/** USFS forest cover, served (and cached) by our backend. */
export function createForestCoverLayer(opacity = 0.3): WebTileLayer {
  return new WebTileLayer({
    id: 'forest-cover',
    urlTemplate: forestTilesTemplate(),
    tileInfo: TileInfo.create({ numLODs: 11 }),
    fullExtent: new Extent({
      xmin: -14244268.17,
      ymin: 2608763.38,
      xmax: -7264863.68,
      ymax: 6737255.24,
      spatialReference: { wkid: 3857 },
    }),
    opacity,
  });
}

const USGS = 'https://basemap.nationalmap.gov/arcgis/rest/services';

export function createBasemap(mode: BasemapMode): Basemap {
  // USGS The National Map (public domain): orthoimagery with US Topo labels, or US Topo alone.
  if (mode === 'map') {
    return new Basemap({
      id: 'detail-map',
      title: 'Map',
      baseLayers: [new TileLayer({ url: `${USGS}/USGSTopo/MapServer` })],
    });
  }
  return new Basemap({
    id: 'satellite-labels',
    title: 'Satellite',
    baseLayers: [new TileLayer({ url: `${USGS}/USGSImageryTopo/MapServer` })],
  });
}

export function createGridMap(basemapMode: BasemapMode, layers: Layer[]): ArcGISMap {
  const map = new ArcGISMap({
    basemap: createBasemap(basemapMode),
    // Flat ground: no hosted elevation service, every map source stays public.
    ground: new Ground({ layers: [] }),
    layers,
  });
  // Dark ground until the imagery arrives, not the default white grid.
  map.ground.surfaceColor = new Color('#09090b');
  return map;
}
