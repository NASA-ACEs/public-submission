import ArcGISMap from '@arcgis/core/Map.js';
import Color from '@arcgis/core/Color.js';
import Basemap from '@arcgis/core/Basemap.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import type Layer from '@arcgis/core/layers/Layer.js';
import OpenStreetMapLayer from '@arcgis/core/layers/OpenStreetMapLayer.js';
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

export function createBasemap(mode: BasemapMode): Basemap {
  if (mode === 'map') {
    return new Basemap({
      id: 'detail-map',
      title: 'Map',
      baseLayers: [new OpenStreetMapLayer()],
    });
  }
  return new Basemap({
    id: 'satellite-labels',
    title: 'Satellite',
    baseLayers: [
      new TileLayer({
        url: 'https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer',
      }),
    ],
    referenceLayers: [
      new TileLayer({
        url: 'https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer',
      }),
    ],
  });
}

export function createGridMap(basemapMode: BasemapMode, layers: Layer[]): ArcGISMap {
  const map = new ArcGISMap({
    basemap: createBasemap(basemapMode),
    ground: 'world-elevation',
    layers,
  });
  // Dark ground until the imagery arrives, not the default white grid.
  map.ground.surfaceColor = new Color('#09090b');
  return map;
}
