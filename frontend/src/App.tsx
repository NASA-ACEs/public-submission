import '@arcgis/core/assets/esri/themes/dark/main.css';
import Camera from '@arcgis/core/Camera.js';
import * as reactiveUtils from '@arcgis/core/core/reactiveUtils.js';
import Extent from '@arcgis/core/geometry/Extent.js';
import Point from '@arcgis/core/geometry/Point.js';
import SpatialReference from '@arcgis/core/geometry/SpatialReference.js';
import type Graphic from '@arcgis/core/Graphic.js';
import type GraphicsLayer from '@arcgis/core/layers/GraphicsLayer.js';
import type WebTileLayer from '@arcgis/core/layers/WebTileLayer.js';
import type ArcGISMap from '@arcgis/core/Map.js';
import MapView from '@arcgis/core/views/MapView.js';
import SceneView from '@arcgis/core/views/SceneView.js';
import { ArrowUp, Clock, Loader2, Menu, Minus, Plus, TriangleAlert, X } from 'lucide-react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import './App.css';
import { ReportsPanel } from './report/ReportsPanel';
import {
  fetchSituation,
  fetchStateLines,
  fetchStates,
  type FireCluster,
  type GridLine,
  type Situation,
  type StateSummary,
  type ThreatLine,
  ownerLabel,
} from './grid/api';
import {
  type BurnabilityLayerId,
  type BurnabilityLayerMeta,
  type BurnabilityMetadata,
  type BurnabilityPointResult,
  type FireGrowth,
  GROWTH_ALONE_COLOR,
  GROWTH_HOURS,
  createBurnabilityRasterLayer,
  createBurnabilityVectorsLayer,
  createFireGrowthLayer,
  drawFireGrowth,
  fetchFireGrowth,
  growthColor,
  defaultLayerId,
  fetchBurnabilityMetadata,
  fetchBurnabilityPoint,
  getResultExtentMercator,
  groupLayers,
  layerHint,
  loadBurnabilityVectors,
  nisarScenes,
  vectorsFollowView,
} from './grid/burnability';
import { DEMO, type DemoScenario } from './demo';
import { mercatorToLonLat, type BasemapMode, type ViewMode } from './grid/geo';
import {
  applyAppearance,
  clearSituation,
  createLayers,
  filterOwners,
  type MapLayers,
  orderedLayers,
  renderFocus,
  renderFireFocus,
  renderCompanyFocus,
  companyExtent,
  renderForestLines,
  renderSituation,
  FIRE_SIZE_STYLE,
  setLines,
  setStateLayersVisible,
  setStates,
} from './grid/layers';
import { createBasemap, createForestCoverLayer, createGridMap } from './grid/powerGrid';

type AppStage = 'overview' | 'state';
type StateTab = 'fires' | 'model' | 'settings';
type FireHover = { id: string; x: number; y: number };

// Floating panel: 12px gap + 280px card + 12px gap.
const SIDEBAR_W = 304;
const FOREST_COLOR_KEY = 'forestLineColor';
const DEFAULT_FOREST_COLOR = '#15803d';
const FOREST_PRESETS = ['#15803d', '#22c55e', '#065f46', '#a3e635', '#0ea5e9'];
const SIZE_ORDER = { large: 0, medium: 1, small: 2 } as const;
const MAP_PADDING = { left: SIDEBAR_W, top: 16, right: 16, bottom: 68 };
const MAP_PADDING_COLLAPSED = { ...MAP_PADDING, left: 16 };
const REPORTS_W = 352; // 12px gap + 328px panel + 12px gap

interface ViewBundle {
  view: MapView | SceneView;
  map: ArcGISMap;
  forestLayer: WebTileLayer;
  layers: MapLayers;
  burnVectors: GraphicsLayer;
  fireGrowth: GraphicsLayer;
  burnRaster: WebTileLayer | null;
}

const US_EXTENT = () =>
  new Extent({
    xmin: -14050000,
    ymin: 2800000,
    xmax: -7400000,
    ymax: 6400000,
    spatialReference: SpatialReference.WebMercator,
  });

const US_CAMERA = () =>
  new Camera({
    position: new Point({
      longitude: -97.5,
      latitude: 35.5,
      z: 5_200_000,
      spatialReference: SpatialReference.WGS84,
    }),
    heading: 0,
    tilt: 4,
  });

const mercExtent = ([xmin, ymin, xmax, ymax]: [number, number, number, number]) =>
  new Extent({ xmin, ymin, xmax, ymax, spatialReference: SpatialReference.WebMercator });

function flyTo(view: MapView | SceneView | undefined, target: Extent, tilt: number, duration = 600) {
  if (!view || view.destroyed) return;
  const p =
    view instanceof SceneView
      ? view.goTo({ target, tilt, heading: 0 }, { duration })
      : view.goTo(target, { duration });
  p.catch(() => {
    // interrupted by another navigation
  });
}

function rankStates(states: StateSummary[], query: string): StateSummary[] {
  const q = query.trim().toLowerCase();
  if (!q) return states;
  const score = (s: StateSummary) => {
    const abbr = s.abbr.toLowerCase();
    const name = s.name.toLowerCase();
    return abbr === q ? 0 : abbr.startsWith(q) ? 1 : name.startsWith(q) ? 2 : 3;
  };
  return states
    .filter((s) => s.name.toLowerCase().includes(q) || s.abbr.toLowerCase().includes(q))
    .sort((a, b) => score(a) - score(b) || a.name.localeCompare(b.name));
}

export function App() {
  const mapHostRef = useRef<HTMLDivElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const bundleRef = useRef<ViewBundle | null>(null);
  const lastExtentRef = useRef<Extent | null>(null);

  const [stage, setStage] = useState<AppStage>('overview');
  const [viewMode, setViewMode] = useState<ViewMode>(DEMO ? '2d' : '3d'); // a demo scenario frames a 2D view
  const [basemapMode, setBasemapMode] = useState<BasemapMode>('satellite');
  const [readyTick, setReadyTick] = useState(0);

  const [states, setStatesList] = useState<StateSummary[]>([]);
  const [statesError, setStatesError] = useState<string | null>(null);
  const [hoveredAbbr, setHoveredAbbr] = useState<string | null>(null);
  const [typedPreviewAbbr, setTypedPreviewAbbr] = useState<string | null>(null);
  const [selectedAbbr, setSelectedAbbr] = useState<string | null>(null);
  const [stateSearch, setStateSearch] = useState('');
  const [searchCursorIdx, setSearchCursorIdx] = useState(0);

  const [lines, setLinesList] = useState<GridLine[]>([]);
  const [owners, setOwners] = useState<Set<string>>(new Set());
  const [providerSearch, setProviderSearch] = useState('');
  const [showForestLines, setShowForestLines] = useState(false);
  const [forestLineColor, setForestLineColor] = useState<string>(() => {
    try {
      return localStorage.getItem(FOREST_COLOR_KEY) || DEFAULT_FOREST_COLOR;
    } catch {
      return DEFAULT_FOREST_COLOR;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(FOREST_COLOR_KEY, forestLineColor);
    } catch {
      /* storage unavailable */
    }
  }, [forestLineColor]);
  const [showCanopy, setShowCanopy] = useState(false);
  const [tab, setTab] = useState<StateTab>('fires');
  const [panelOpen, setPanelOpen] = useState(true);
  const [reportsOpen, setReportsOpen] = useState(false);
  const [companyLines, setCompanyLines] = useState<ThreatLine[] | null>(null);
  const [hoverFire, setHoverFire] = useState<FireHover | null>(null);
  const fireHoverRef = useRef<(h: FireHover | null) => void>(() => {});
  const fireClickRef = useRef<(id: string | null) => void>(() => {});
  const growthClickRef = useRef<(lon: number, lat: number) => void>(() => {});
  const growthOnRef = useRef(false);
  const pinnedCardRef = useRef<HTMLDivElement | null>(null);
  const [forestOnly, setForestOnly] = useState(true);

  const [fireHours, setFireHours] = useState(72);
  const [radiusM, setRadiusM] = useState(3000);
  const [situation, setSituation] = useState<Situation | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [retryTick, setRetryTick] = useState(0);
  const [focusedClusterId, setFocusedClusterId] = useState<string | null>(null);

  const [burnMeta, setBurnMeta] = useState<BurnabilityMetadata | null>(null);
  const [burnLayerId, setBurnLayerId] = useState<BurnabilityLayerId>('susceptibility_index');
  const [showBurnRaster, setShowBurnRaster] = useState(false);
  const [showBurnVectors, setShowBurnVectors] = useState(false);
  const [pointSample, setPointSample] = useState<BurnabilityPointResult | null>(null);
  // Fire growth from a clicked point (model results that have the layers for it).
  const [showGrowth, setShowGrowth] = useState(false);
  const [growthPoint, setGrowthPoint] = useState<{ lon: number; lat: number } | null>(null);
  const [growthAnswer, setGrowthAnswer] = useState<{ key: string; growth: FireGrowth | null; error: string | null } | null>(null);
  const [growthCustomWind, setGrowthCustomWind] = useState(false);
  const [growthWindSpeed, setGrowthWindSpeed] = useState('10');
  const [growthWindFrom, setGrowthWindFrom] = useState('45');
  const [growthFuelAge, setGrowthFuelAge] = useState(true);
  // The same fire grown twice: on fuels scaled by NISAR's fuel state, and on the fuel map alone.
  const [growthCompare, setGrowthCompare] = useState(false);
  const [growthAlone, setGrowthAlone] = useState<{ key: string; growth: FireGrowth | null; error: string | null } | null>(null);
  const [nisarOn, setNisarOn] = useState(true); // which of the two estimates is drawn as the fire
  const [demoNote, setDemoNote] = useState(true);
  const demoRanRef = useRef(false);
  const growthBlockRef = useRef<HTMLDivElement | null>(null);
  const demoScrolledRef = useRef(false);

  const selectedState = useMemo(
    () => states.find((s) => s.abbr === selectedAbbr) ?? null,
    [states, selectedAbbr],
  );
  const hoveredState = useMemo(
    () => states.find((s) => s.abbr === hoveredAbbr) ?? null,
    [states, hoveredAbbr],
  );

  // Refs read from ArcGIS event handlers without recreating the view.
  const stageRef = useRef(stage);
  const statesRef = useRef(states);
  const basemapRef = useRef(basemapMode);
  const burnMetaRef = useRef(burnMeta);
  const hoveredRef = useRef(hoveredAbbr);
  const selectedRef = useRef(selectedAbbr);
  useEffect(() => {
    stageRef.current = stage;
    statesRef.current = states;
    basemapRef.current = basemapMode;
    burnMetaRef.current = burnMeta;
    hoveredRef.current = hoveredAbbr;
    selectedRef.current = selectedAbbr;
  }, [stage, states, basemapMode, burnMeta, hoveredAbbr, selectedAbbr]);

  const pickState = useCallback((st: StateSummary, fly = true) => {
    setSelectedAbbr(st.abbr);
    setTypedPreviewAbbr(null);
    setOwners(new Set(st.providers.map((p) => p.owner)));
    setProviderSearch('');
    if (fly) flyTo(bundleRef.current?.view, mercExtent(st.extent).expand(1.3), 12, 480);
  }, []);

  // Two estimates of one fire: with NISAR's fuel state and on the fuel map alone. With NISAR off the second one is
  // the fire drawn. In a demo scenario the same switch also hides the model layer: the satellite image shows no scar.
  const compareOn = growthCompare && !!burnMeta?.capabilities?.spread_fuel_state;
  const nisarShown = !(compareOn && !nisarOn);
  const demo = DEMO && burnMeta?.result_id === DEMO.result ? DEMO : null;
  const demoSwitch = !!demo?.fire && demoNote && stage === 'state' && !!burnMeta?.capabilities?.spread_fuel_state;
  const hideRaster = demoSwitch && !nisarShown;

  // Initial data: states + burnability model metadata, both from our backend.
  useEffect(() => {
    const ac = new AbortController();
    fetchStates()
      .then((list) => !ac.signal.aborted && setStatesList(list))
      .catch((err) => !ac.signal.aborted && setStatesError(String(err)));
    void fetchBurnabilityMetadata(ac.signal).then((m) => {
      if (ac.signal.aborted) return;
      setBurnMeta(m);
      if (m) setBurnLayerId(defaultLayerId(m));
    });
    return () => ac.abort();
  }, []);

  // ArcGIS view lifecycle (rebuilt only on 2D/3D switch).
  useEffect(() => {
    const container = mapHostRef.current;
    if (!container) return;
    let disposed = false;
    let dispose: (() => void) | null = null;

    // Deferred so React StrictMode's double mount doesn't abort basemap loads.
    const timer = window.setTimeout(() => {
      if (disposed) return;
      const layers = createLayers();
      applyAppearance(layers, basemapRef.current);
      const forestLayer = createForestCoverLayer(basemapRef.current === 'map' ? 0.22 : 0.3);
      const burnVectors = createBurnabilityVectorsLayer();
      const fireGrowth = createFireGrowthLayer();
      const map = createGridMap(basemapRef.current, [forestLayer, ...orderedLayers(layers), burnVectors, fireGrowth]);
      const prevExtent = lastExtentRef.current;

      const view =
        viewMode === '3d'
          ? new SceneView({
              container,
              map,
              camera: prevExtent ? undefined : US_CAMERA(),
              extent: prevExtent ?? undefined,
              padding: MAP_PADDING,
              qualityProfile: 'medium',
              environment: {
                background: { type: 'color', color: '#09090b' },
                starsEnabled: false,
                atmosphereEnabled: true,
                lighting: { directShadowsEnabled: false },
              },
              ui: { components: [] },
            })
          : new MapView({
              container,
              map,
              extent: prevExtent ?? US_EXTENT(),
              padding: MAP_PADDING,
              constraints: { minZoom: 3, maxZoom: 18 },
              ui: { components: [] },
            });

      const bundle: ViewBundle = { view, map, forestLayer, layers, burnVectors, fireGrowth, burnRaster: null };
      let active = true;
      let hitPending = false;
      let lastEvent: { x: number; y: number } | null = null;

      // Hover: tooltip moves via direct DOM writes; React only re-renders when the state changes.
      const moveHandle = view.on('pointer-move', (event) => {
        const tip = tooltipRef.current;
        if (stageRef.current !== 'overview') {
          if (tip) tip.hidden = true;
          lastEvent = { x: event.x, y: event.y };
          if (hitPending) return;
          hitPending = true;
          requestAnimationFrame(() => {
            const ev = lastEvent;
            if (!ev || !active) {
              hitPending = false;
              return;
            }
            view
              .hitTest(ev, { include: [layers.fires] })
              .then((hit) => {
                if (!active || stageRef.current === 'overview') return;
                const res = hit.results.find((r): r is { graphic: Graphic } & typeof r => 'graphic' in r);
                const id = res?.graphic.attributes?.CID ? String(res.graphic.attributes.CID) : null;
                container.style.cursor = id ? 'pointer' : growthOnRef.current ? 'crosshair' : '';
                fireHoverRef.current(id ? { id, x: ev.x, y: ev.y } : null);
              })
              .catch(() => {})
              .finally(() => {
                hitPending = false;
              });
          });
          return;
        }
        if (tip) {
          tip.style.transform = `translate(${Math.min(window.innerWidth - 200, event.x + 12)}px, ${Math.max(8, event.y - 10)}px)`;
        }
        lastEvent = { x: event.x, y: event.y };
        if (hitPending) return;
        hitPending = true;
        requestAnimationFrame(() => {
          const ev = lastEvent;
          if (!ev || !active) {
            hitPending = false;
            return;
          }
          view
            .hitTest(ev, { include: layers.states })
            .then((hit) => {
              if (!active || stageRef.current !== 'overview') return;
              const res = hit.results.find((r): r is { graphic: Graphic } & typeof r => 'graphic' in r);
              const abbr = res?.graphic.attributes?.ABBR ? String(res.graphic.attributes.ABBR) : null;
              container.style.cursor = abbr ? 'pointer' : '';
              if (abbr !== hoveredRef.current) {
                hoveredRef.current = abbr;
                setHoveredAbbr(abbr);
              }
              if (tip) tip.hidden = !abbr || abbr === selectedRef.current || ev.x <= SIDEBAR_W;
            })
            .catch(() => {})
            .finally(() => {
              hitPending = false;
            });
        });
      });

      const leaveHandle = view.on('pointer-leave', () => {
        if (tooltipRef.current) tooltipRef.current.hidden = true;
        fireHoverRef.current(null);
      });

      const clickHandle = view.on('click', async (event) => {
        if (stageRef.current === 'overview') {
          const hit = await view.hitTest(event, { include: layers.states });
          const res = hit.results.find((r): r is { graphic: Graphic } & typeof r => 'graphic' in r);
          const found = statesRef.current.find((s) => s.abbr === res?.graphic.attributes?.ABBR);
          if (found) pickState(found);
          return;
        }
        const fireHit = await view.hitTest(event, { include: [layers.fires] });
        const fireRes = fireHit.results.find((r): r is { graphic: Graphic } & typeof r => 'graphic' in r);
        const fireId = fireRes?.graphic.attributes?.CID ? String(fireRes.graphic.attributes.CID) : null;
        fireClickRef.current(fireId);
        if (fireId) return;
        const meta = burnMetaRef.current;
        if (event.mapPoint && meta) {
          const [lon, lat] = event.mapPoint.spatialReference?.isWGS84
            ? [event.mapPoint.x, event.mapPoint.y]
            : mercatorToLonLat(event.mapPoint.x, event.mapPoint.y);
          const [x0, y0, x1, y1] = meta.bbox_wgs84;
          growthClickRef.current(lon, lat); // outside the result too: the backend says why no fire grows there
          if (lon >= x0 && lon <= x1 && lat >= y0 && lat <= y1) {
            const r = await fetchBurnabilityPoint(meta, lon, lat);
            if (r?.valid) setPointSample(r);
          }
        }
      });

      void view.when(() => {
        if (!active) return;
        bundleRef.current = bundle;
        if (view instanceof SceneView) {
          const target = prevExtent ?? (stageRef.current === 'overview' ? US_EXTENT().expand(1.12) : null);
          if (target) void view.goTo({ target, tilt: stageRef.current === 'overview' ? 4 : 16 }, { duration: 0 });
        }
        setReadyTick((t) => t + 1);
      });

      dispose = () => {
        active = false;
        moveHandle.remove();
        leaveHandle.remove();
        clickHandle.remove();
        container.style.cursor = '';
        if (view.extent) lastExtentRef.current = view.extent.clone();
        if (bundleRef.current === bundle) bundleRef.current = null;
        view.destroy();
      };
    }, 0);

    return () => {
      disposed = true;
      window.clearTimeout(timer);
      dispose?.();
    };
  }, [viewMode, pickState]);

  // State polygons for the overview picker.
  useEffect(() => {
    const b = bundleRef.current;
    if (b && states.length) void setStates(b.layers, states);
  }, [readyTick, states]);

  // Basemap, layer visibility and appearance.
  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    const inState = stage === 'state';
    const basemapId = basemapMode === 'map' ? 'detail-map' : 'satellite-labels';
    if (b.map.basemap?.id !== basemapId) b.map.basemap = createBasemap(basemapMode);
    applyAppearance(b.layers, basemapMode);
    b.forestLayer.opacity = basemapMode === 'map' ? 0.22 : 0.3;
    b.forestLayer.visible = inState && showCanopy;
    b.burnVectors.visible = inState && showBurnVectors;
    setStateLayersVisible(b.layers, inState, showForestLines);
  }, [readyTick, basemapMode, stage, showCanopy, showForestLines, showBurnVectors]);

  // Hover / selection highlight in the overview.
  useEffect(() => {
    const b = bundleRef.current;
    if (!b || stage !== 'overview') return;
    renderFocus(b.layers, states, hoveredAbbr ?? typedPreviewAbbr, selectedAbbr, basemapMode);
  }, [readyTick, stage, states, hoveredAbbr, typedPreviewAbbr, selectedAbbr, basemapMode]);

  // Base transmission lines of the opened state.
  useEffect(() => {
    const b = bundleRef.current;
    if (b && stage === 'state') void setLines(b.layers, lines);
  }, [readyTick, stage, lines]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b || stage !== 'state' || !selectedState) return;
    filterOwners(b.layers, owners, selectedState.providers.length);
  }, [readyTick, stage, owners, selectedState]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b || stage !== 'state') return;
    if (showForestLines) renderForestLines(b.layers, lines, owners, forestLineColor);
    else b.layers.forestLines.removeAll();
  }, [readyTick, stage, lines, owners, showForestLines, forestLineColor]);

  // Risk picture from the backend. Debounced so rapid operator toggles send one request.
  const ownersKey = useMemo(() => [...owners].sort().join('\u0000'), [owners]);
  useEffect(() => {
    if (stage !== 'state' || !selectedState) return;
    const ac = new AbortController();
    const all = owners.size >= selectedState.providers.length;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setLoadError(null);
      fetchSituation(
        selectedState.abbr,
        { hours: fireHours, radiusM, owners: all ? null : [...owners], forestOnly },
        ac.signal,
      )
        .then((s) => {
          if (ac.signal.aborted) return;
          setSituation(s);
          setFocusedClusterId((prev) => (s.fires.clusters.some((c) => c.id === prev) ? prev : null));
        })
        .catch((err) => {
          if (!ac.signal.aborted) setLoadError(String(err));
        })
        .finally(() => {
          if (!ac.signal.aborted) setLoading(false);
        });
    }, 200);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
    // ownersKey stands in for `owners` (Set identity changes on every toggle).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, selectedState, fireHours, radiusM, ownersKey, forestOnly, retryTick]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    if (stage === 'state' && situation && situation.state === selectedAbbr) {
      renderSituation(b.layers, situation, basemapMode);
    } else {
      clearSituation(b.layers);
    }
  }, [readyTick, stage, situation, selectedAbbr, basemapMode]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    const show = stage === 'state' && situation && situation.state === selectedAbbr;
    renderFireFocus(b.layers, show ? situation : null, show ? focusedClusterId : null);
  }, [readyTick, stage, situation, selectedAbbr, focusedClusterId]);

  // Selected operator in the reports panel: highlight its fires and frame them.
  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    const show = stage === 'state' && situation && situation.state === selectedAbbr;
    renderCompanyFocus(b.layers, show ? situation : null, show ? companyLines : null);
  }, [readyTick, stage, situation, selectedAbbr, companyLines]);
  const companyOwner = companyLines?.[0]?.owner ?? null;
  useEffect(() => {
    if (!situation || !companyLines?.length) return;
    const ext = companyExtent(situation, companyLines);
    if (ext) flyTo(bundleRef.current?.view, mercExtent(ext).expand(1.25), 20, 700);
    // Frame once per company, not on every situation refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyOwner]);

  // Panel collapses after Open; the map reclaims the space.
  const panelCollapsed = stage === 'state' && !panelOpen;
  useEffect(() => {
    const view = bundleRef.current?.view;
    if (!view || view.destroyed) return;
    const base = panelCollapsed ? MAP_PADDING_COLLAPSED : MAP_PADDING;
    view.padding = { ...base, right: stage === 'state' && reportsOpen ? REPORTS_W : base.right };
  }, [readyTick, panelCollapsed, reportsOpen, stage]);

  // Pinned fire card follows its fire while the map moves (direct DOM writes, no re-render).
  useEffect(() => {
    const view = bundleRef.current?.view;
    const c = situation?.fires.clusters.find((x) => x.id === focusedClusterId);
    if (!view || !c || stage !== 'state') return;
    const anchor = new Point({ x: c.centroid[0], y: c.centroid[1], spatialReference: SpatialReference.WebMercator });
    const place = () => {
      const el = pinnedCardRef.current;
      if (!el || view.destroyed) return;
      const pt = view.toScreen(anchor);
      const visible = !!pt && pt.x > 0 && pt.y > 0 && pt.x < view.width && pt.y < view.height;
      el.hidden = !visible;
      if (pt && visible) el.style.transform = cardTransform(pt.x, pt.y, el);
    };
    place();
    const handle = reactiveUtils.watch(() => [view.extent, view.width, view.height], place);
    return () => handle.remove();
  }, [readyTick, stage, situation, focusedClusterId]);

  // Burnability raster overlay (backend tiles).
  useEffect(() => {
    const b = bundleRef.current;
    if (!b || stage !== 'state' || !showBurnRaster || hideRaster || !burnMeta) return;
    const overlay = createBurnabilityRasterLayer(burnMeta, burnLayerId, 0.72);
    b.burnRaster = overlay;
    b.map.add(overlay, Math.max(0, b.map.layers.indexOf(b.layers.lines)));
    return () => {
      if (!b.map.destroyed) b.map.remove(overlay);
      overlay.destroy();
      if (b.burnRaster === overlay) b.burnRaster = null;
    };
  }, [readyTick, stage, showBurnRaster, hideRaster, burnLayerId, burnMeta]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b || stage !== 'state' || !showBurnVectors || !burnMeta) {
      b?.burnVectors.removeAll();
      return;
    }
    let ac = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () => {
      ac.abort();
      ac = new AbortController();
      const e = b.view.extent;
      const box = e ? ([...mercatorToLonLat(e.xmin, e.ymin), ...mercatorToLonLat(e.xmax, e.ymax)] as [number, number, number, number]) : undefined;
      void loadBurnabilityVectors(b.burnVectors, burnMeta, b.view.zoom ?? 10, ac.signal, box);
    };
    load();
    // A model result's arrows are sampled for the view: ask again once the map has come to rest.
    const handle = reactiveUtils.watch(
      () => b.view.stationary,
      (still) => {
        clearTimeout(timer);
        if (still && vectorsFollowView(burnMeta)) timer = setTimeout(load, 250);
      },
    );
    return () => {
      handle.remove();
      clearTimeout(timer);
      ac.abort();
    };
  }, [readyTick, stage, showBurnVectors, burnMeta]);

  // Fire growth: ask the backend when the point or an option changes, draw what it answers.
  const growthOn = showBurnRaster && showGrowth && !!burnMeta?.capabilities?.spread;
  const growthWind = useMemo(() => {
    if (!growthCustomWind) return null;
    const [speed, from] = [Number(growthWindSpeed), Number(growthWindFrom)];
    const ok = growthWindSpeed !== '' && growthWindFrom !== '' && speed >= 0 && speed <= 40 && from >= 0 && from <= 360;
    return ok ? { speed, from } : undefined;
  }, [growthCustomWind, growthWindSpeed, growthWindFrom]);
  const growthKey =
    growthOn && growthPoint && growthWind !== undefined
      ? JSON.stringify([burnMeta?.result_id, growthPoint, growthWind, growthFuelAge])
      : null;
  useEffect(() => {
    if (!growthKey || !burnMeta || !growthPoint || growthWind === undefined) return;
    const ac = new AbortController();
    void fetchFireGrowth(burnMeta, growthPoint.lon, growthPoint.lat, { wind: growthWind, fuelAge: growthFuelAge }, ac.signal).then(
      (r) => {
        if (!ac.signal.aborted) setGrowthAnswer({ key: growthKey, ...r });
      },
    );
    return () => ac.abort();
  }, [growthKey, burnMeta, growthPoint, growthWind, growthFuelAge]);
  const growthShown = growthOn && growthAnswer?.key === growthKey ? growthAnswer : null;
  const growthBusy = growthKey !== null && !growthShown;

  useEffect(() => {
    if (!compareOn || !growthKey || !burnMeta || !growthPoint || growthWind === undefined) return;
    const ac = new AbortController();
    void fetchFireGrowth(
      burnMeta,
      growthPoint.lon,
      growthPoint.lat,
      { wind: growthWind, fuelAge: growthFuelAge, fuelState: false },
      ac.signal,
    ).then((r) => {
      if (!ac.signal.aborted) setGrowthAlone({ key: growthKey, ...r });
    });
    return () => ac.abort();
  }, [compareOn, growthKey, burnMeta, growthPoint, growthWind, growthFuelAge]);
  const aloneShown = compareOn && growthAlone?.key === growthKey ? growthAlone : null;
  const growthPair = useMemo(() => comparedAreas(growthShown?.growth, aloneShown?.growth), [growthShown, aloneShown]);
  // NISAR off: the fire of the fuel map alone is the one drawn, and the NISAR one becomes the dashed outline.
  const showNisar = (on: boolean) => {
    if (!on) setGrowthCompare(true);
    setNisarOn(on);
  };
  const growthMain = nisarShown ? growthShown : aloneShown;
  const growthOther = compareOn ? (nisarShown ? aloneShown : growthShown) : null;

  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    const on = stage === 'state' && growthOn;
    b.fireGrowth.visible = on;
    drawFireGrowth(b.fireGrowth, on ? (growthMain?.growth ?? null) : null, on ? growthPoint : null, on ? (growthOther?.growth ?? null) : null);
  }, [readyTick, stage, growthOn, growthMain, growthOther, growthPoint]);

  const enterState = useCallback(async (st: StateSummary, fly = true): Promise<boolean> => {
    setLoadError(null);
    setOpening(true);
    try {
      const stateLines = await fetchStateLines(st.abbr);
      setLinesList(stateLines);
    } catch (err) {
      setLoadError(String(err));
      return false;
    } finally {
      setOpening(false);
    }
    setSituation(null);
    setStage('state');
    setTab('fires');
    setPanelOpen(false);
    setFocusedClusterId(null);
    setHoveredAbbr(null);
    setShowBurnRaster(false);
    if (tooltipRef.current) tooltipRef.current.hidden = true;
    if (fly) flyTo(bundleRef.current?.view, mercExtent(st.extent).expand(1.12), 16, 650);
    return true;
  }, []);
  const enterSelectedState = useCallback(async () => {
    if (selectedState) await enterState(selectedState);
  }, [selectedState, enterState]);

  // Demo scenario (?demo= or VITE_DEMO): once the map, the states and the scenario's result are there, open the
  // state and the model tab, set the layer and the view, and start the prepared fire. Runs once; then the app is
  // the user's again.
  const runDemo = useCallback(
    async (d: DemoScenario, st: StateSummary) => {
      pickState(st, false);
      if (!(await enterState(st, false))) return; // the scenario frames its own view
      setPanelOpen(true);
      setTab('model');
      setShowBurnRaster(true);
      setBurnLayerId(d.layer);
      if (d.fire) {
        setShowGrowth(true);
        setGrowthCustomWind(true);
        setGrowthWindSpeed(String(d.fire.wind.speed));
        setGrowthWindFrom(String(d.fire.wind.from));
        setGrowthPoint({ lon: d.fire.lon, lat: d.fire.lat });
        setGrowthCompare(true);
      }
      const view = bundleRef.current?.view;
      if (view && !view.destroyed) void view.goTo({ center: d.center, zoom: d.zoom }, { duration: 900 }).catch(() => {});
    },
    [pickState, enterState],
  );
  // The scenario's switch answers to the N key as well.
  useEffect(() => {
    if (!demoSwitch) return;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.key.toLowerCase() !== 'n' || e.ctrlKey || e.metaKey || e.altKey || (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      if (nisarShown) setGrowthCompare(true);
      setNisarOn(!nisarShown);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [demoSwitch, nisarShown]);
  // The scenario's two fires have been grown: bring their figures into view in the panel, once.
  useEffect(() => {
    if (!demo?.fire || !growthPair || demoScrolledRef.current) return;
    demoScrolledRef.current = true;
    growthBlockRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [demo, growthPair]);
  useEffect(() => {
    if (!demo || demoRanRef.current || !readyTick) return;
    const st = states.find((s) => s.abbr === demo.state);
    if (!st) return;
    demoRanRef.current = true;
    void runDemo(demo, st);
  }, [demo, readyTick, states, runDemo]);

  const backToOverview = useCallback(() => {
    setStage('overview');
    setPanelOpen(true);
    setReportsOpen(false);
    setHoverFire(null);
    setFocusedClusterId(null);
    setSituation(null);
    setPointSample(null);
    setLinesList([]);
    flyTo(bundleRef.current?.view, US_EXTENT().expand(1.12), 4, 650);
  }, []);

  const resetToAllStates = useCallback(() => {
    setSelectedAbbr(null);
    setTypedPreviewAbbr(null);
    setStateSearch('');
    setSearchCursorIdx(0);
    flyTo(bundleRef.current?.view, US_EXTENT().expand(1.12), 4, 650);
  }, []);

  const filteredStates = useMemo(() => rankStates(states, stateSearch), [states, stateSearch]);

  const handleStateSearchChange = (next: string) => {
    setStateSearch(next);
    setSearchCursorIdx(0);
    const first = next.trim() ? (rankStates(states, next)[0]?.abbr ?? null) : null;
    setTypedPreviewAbbr(first);
    if (selectedAbbr && first !== selectedAbbr) setSelectedAbbr(null);
  };

  const filteredProviders = useMemo(() => {
    if (!selectedState) return [];
    const q = providerSearch.trim().toLowerCase();
    return q
      ? selectedState.providers.filter((p) => ownerLabel(p.owner).toLowerCase().includes(q))
      : selectedState.providers;
  }, [selectedState, providerSearch]);

  const sortedFires = useMemo(
    () =>
      [...(situation?.fires.clusters ?? [])].sort(
        (a, b) => SIZE_ORDER[a.sizeClass ?? 'small'] - SIZE_ORDER[b.sizeClass ?? 'small'] || b.areaAcres - a.areaAcres,
      ),
    [situation],
  );
  const visibleFires = sortedFires;
  const windByCluster = useMemo(
    () => new Map((situation?.spread ?? []).map((e) => [e.clusterId, e.wind])),
    [situation],
  );
  const fireFilter = situation?.fires.filter;
  const droppedDetections = fireFilter ? fireFilter.totalDetections - fireFilter.keptDetections : 0;

  const flyToCluster = (cl: FireCluster) => {
    setFocusedClusterId(cl.id);
    const [x0, y0, x1, y1] = cl.bbox;
    flyTo(bundleRef.current?.view, mercExtent([x0 - 6500, y0 - 6500, x1 + 6500, y1 + 6500]), 20);
  };


  const flyToLine = (line: ThreatLine) => {
    flyTo(bundleRef.current?.view, mercExtent(line.bbox).expand(1.4), 20);
  };

  const flyToModelArea = () => {
    if (!burnMeta) return;
    setShowBurnRaster(true);
    flyTo(bundleRef.current?.view, getResultExtentMercator(burnMeta).expand(1.2), 22, 650);
  };

  const burnLayer = burnMeta?.layers.find((l) => l.id === burnLayerId);
  const nisar = burnMeta && !burnMeta.synthetic ? nisarScenes(burnMeta) : null;

  const stepZoom = (delta: number) => {
    const view = bundleRef.current?.view;
    if (!view || view.destroyed) return;
    if (view instanceof MapView) {
      void view.goTo({ zoom: Math.max(3, Math.min(18, view.zoom + delta)) }, { duration: 200 });
    } else if (view.camera) {
      const cam = view.camera.clone();
      cam.position = new Point({
        x: cam.position.x,
        y: cam.position.y,
        z: Math.max(800, (cam.position.z ?? 500_000) * (delta > 0 ? 0.58 : 1.65)),
        spatialReference: cam.position.spatialReference,
      });
      void view.goTo(cam, { duration: 200 });
    }
  };

  const toggleOwner = (owner: string) => {
    setOwners((prev) => {
      const next = new Set(prev);
      if (next.has(owner)) next.delete(owner);
      else next.add(owner);
      return next;
    });
  };

  const clusterById = useMemo(
    () => new Map((situation?.fires.clusters ?? []).map((c) => [c.id, c])),
    [situation],
  );
  const spreadById = useMemo(
    () => new Map((situation?.spread ?? []).map((e) => [e.clusterId, e])),
    [situation],
  );
  fireHoverRef.current = (h) =>
    setHoverFire((prev) => {
      if (!h) return null;
      if (prev && prev.id === h.id && Math.abs(prev.x - h.x) + Math.abs(prev.y - h.y) < 3) return prev;
      return h;
    });
  growthOnRef.current = growthOn;
  growthClickRef.current = (lon, lat) => {
    if (growthOn) setGrowthPoint({ lon, lat });
  };
  fireClickRef.current = (id) => {
    const cl = id ? clusterById.get(id) : undefined;
    if (!cl) {
      setFocusedClusterId(null);
      return;
    }
    flyToCluster(cl);
    setHoverFire(null);
  };
  const pinnedFire = focusedClusterId ? clusterById.get(focusedClusterId) : undefined;
  const hoveredFire = hoverFire && hoverFire.id !== focusedClusterId ? clusterById.get(hoverFire.id) : undefined;

  // Lines each fire threatens, per tier: the same grouping the reports use.
  const threatByFire = useMemo(() => {
    const by = new Map<string, FireThreat>();
    for (const l of situation?.threats.lines ?? []) {
      if (!l.fire?.id || (l.tier !== 'SHUT OFF' && l.tier !== 'PSPS')) continue;
      const t = by.get(l.fire.id) ?? { shutoff: 0, shutoffKm: 0, psps: 0, pspsKm: 0 };
      if (l.tier === 'SHUT OFF') {
        t.shutoff += 1;
        t.shutoffKm += l.sectionKm ?? 0;
      } else {
        t.psps += 1;
        t.pspsKm += l.sectionKm ?? 0;
      }
      by.set(l.fire.id, t);
    }
    return by;
  }, [situation]);

  // Esc lets go of the pinned fire.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setFocusedClusterId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const burnGroups = useMemo(() => (burnMeta ? groupLayers(burnMeta.layers) : []), [burnMeta]);
  const burnGroup = burnGroups.find((g) => g.layers.some((l) => l.id === burnLayerId)) ?? burnGroups[0];
  const hasNisar = !!nisar && (nisar.sme2 > 0 || nisar.gcov > 0);
  const modelTab = burnMeta ? (hasNisar ? 'NISAR' : 'Fuel risk') : null;
  const openTab = (id: StateTab) => {
    setTab(id);
    // The model area can be a small box inside the state: go there the first time the tab is opened.
    if (id === 'model' && !showBurnRaster) flyToModelArea();
  };
  const sources = `NASA ${hasNisar ? 'NISAR, ' : ''}VIIRS, NIFC, NOAA, HIFLD, USFS`;
  const fireCount = situation?.fires.clusters.length ?? 0;

  return (
    <div className="app-shell">
      <div ref={mapHostRef} className="map-canvas" />

      {demo && demoNote && stage === 'state' && (
        <aside className="demo-note" aria-label="Demo scenario">
          <div className="demo-note-head">
            <span>{demo.title}</span>
            <button type="button" className="icon-btn" onClick={() => setDemoNote(false)} aria-label="Close">
              <X size={14} />
            </button>
          </div>
          <p>{demo.text}</p>
          {demoSwitch && (
            <>
              <div className="demo-switch" role="group" aria-label="Estimate">
                <button type="button" className={nisarShown ? 'is-active' : ''} aria-pressed={nisarShown} onClick={() => showNisar(true)}>
                  <b>NISAR ON</b>
                  <span>Our estimate (with NISAR fuel state)</span>
                </button>
                <button type="button" className={nisarShown ? '' : 'is-active'} aria-pressed={!nisarShown} onClick={() => showNisar(false)}>
                  <b>NISAR OFF</b>
                  <span>Typical estimate (fuel map alone)</span>
                </button>
              </div>
              {growthPair ? (
                <div className="demo-figure">
                  <span className="unit">{growthPair.hours} h</span>
                  <span className="fig">{fmtValue(nisarShown ? growthPair.nisar : growthPair.alone)}</span>
                  <span className="unit">ha</span>
                  <span className="demo-figure-note">{comparedText(growthPair, nisarShown)}</span>
                </div>
              ) : (
                <div className="burn-legend-hint">
                  <Loader2 size={11} className="spin" /> Growing both fires
                </div>
              )}
              <ul className="demo-legend">
                {GROWTH_HOURS.map((h) => (
                  <li key={h}>
                    <i className="growth-swatch" style={{ background: growthColor(h) }} />
                    burned after {h} h
                  </li>
                ))}
                <li>
                  <i className="growth-line is-dashed" style={{ borderColor: GROWTH_ALONE_COLOR }} />
                  {GROWTH_HOURS[GROWTH_HOURS.length - 1]} h {nisarShown ? 'without NISAR' : 'with NISAR'}
                </li>
                <li>
                  <X size={11} strokeWidth={3} aria-hidden /> ignition
                </li>
                <li className="demo-key">
                  <kbd>N</kbd> switches
                </li>
              </ul>
              {nisarShown && burnLayer ? (
                <Legend
                  layer={burnLayer}
                  hint="Background: fuel state from NISAR’s L-band radar. Yellow: mature fuel (about 1). Purple and dark blue: regrowth after fire, less fuel."
                />
              ) : (
                <div className="burn-legend-hint">Background: the satellite image alone. The scar cannot be seen in it.</div>
              )}
            </>
          )}
          {demo.views && burnMeta && (
            <>
              <div className="btn-group is-fill" role="group" aria-label="Layer">
                {demo.views.map((v) => (
                  <button
                    key={v.layer}
                    type="button"
                    className={v.layer === burnLayerId ? 'is-active' : ''}
                    aria-pressed={v.layer === burnLayerId}
                    onClick={() => {
                      setShowBurnRaster(true);
                      setBurnLayerId(v.layer);
                    }}
                  >
                    {v.label}
                  </button>
                ))}
              </div>
              {demo.views.filter((v) => v.layer === burnLayerId && burnLayer).map((v) => (
                <Legend key={v.layer} layer={burnLayer as BurnabilityLayerMeta} hint={v.legend} />
              ))}
            </>
          )}
          <p className="demo-note-small">{demo.small}</p>
        </aside>
      )}

      {panelCollapsed && selectedState && (
        <button type="button" className="panel-pill" onClick={() => setPanelOpen(true)} aria-label="Open panel">
          <span className="pill-icon" aria-hidden>
            {loading ? <Loader2 size={15} className="spin" /> : loadError ? <TriangleAlert size={15} /> : <Menu size={15} />}
          </span>
          <span className="pill-name">{selectedState.name}</span>
          {loadError ? (
            <span className="pill-stat">offline</span>
          ) : (
            situation && (
              <span className="pill-stat">
                <b>{fireCount}</b> {forestOnly ? 'forest ' : ''}
                {fireCount === 1 ? 'fire' : 'fires'}
              </span>
            )
          )}
        </button>
      )}

      <aside
        className={`sidebar ${panelCollapsed ? 'is-collapsed' : ''} ${stage === 'overview' && selectedState ? 'is-compact' : ''}`}
        aria-hidden={panelCollapsed}
        inert={panelCollapsed}
      >
        {stage === 'overview' && !selectedState && (
          <>
            <div className="sidebar-top">
              <input
                type="text"
                className="input"
                placeholder="Search states"
                aria-label="Search states"
                value={stateSearch}
                onChange={(e) => handleStateSearchChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown' && filteredStates.length > 0) {
                    e.preventDefault();
                    const i = Math.min(filteredStates.length - 1, searchCursorIdx + 1);
                    setSearchCursorIdx(i);
                    setTypedPreviewAbbr(filteredStates[i]?.abbr ?? null);
                  } else if (e.key === 'ArrowUp' && filteredStates.length > 0) {
                    e.preventDefault();
                    const i = Math.max(0, searchCursorIdx - 1);
                    setSearchCursorIdx(i);
                    setTypedPreviewAbbr(filteredStates[i]?.abbr ?? null);
                  } else if (e.key === 'Enter' && filteredStates.length > 0) {
                    e.preventDefault();
                    pickState(filteredStates[searchCursorIdx] ?? filteredStates[0]);
                  } else if (e.key === 'Escape') {
                    resetToAllStates();
                  }
                }}
              />
            </div>
            <div className="sidebar-scroll">
              {statesError ? (
                <p className="notice">
                  <TriangleAlert size={14} /> Backend unreachable
                  <button type="button" className="link" onClick={() => window.location.reload()}>
                    Retry
                  </button>
                </p>
              ) : states.length === 0 ? (
                <p className="empty">Loading states</p>
              ) : filteredStates.length === 0 ? (
                <p className="empty">No matching states</p>
              ) : (
                <>
                  <div className="list-head" aria-hidden>
                    <span>State</span>
                    <span>Lines in forest</span>
                  </div>
                  {filteredStates.map((st, idx) => {
                    const active =
                      st.abbr === hoveredAbbr || (idx === searchCursorIdx && stateSearch.trim().length > 0);
                    return (
                      <button
                        key={st.abbr}
                        type="button"
                        className={`item ${active ? 'is-active' : ''}`}
                        onMouseEnter={() => {
                          setHoveredAbbr(st.abbr);
                          setSearchCursorIdx(idx);
                        }}
                        onMouseLeave={() => setHoveredAbbr(null)}
                        onClick={() => pickState(st)}
                      >
                        <span className="item-main">{st.name}</span>
                        <span className="item-side">
                          {Math.round(st.forestKm).toLocaleString()} <span className="unit">km</span>
                        </span>
                      </button>
                    );
                  })}
                </>
              )}
            </div>
            <div className="sidebar-foot">{sources}</div>
          </>
        )}

        {stage === 'overview' && selectedState && (
          <div className="sidebar-pad">
            <button type="button" className="link" onClick={resetToAllStates}>
              All states
            </button>
            <h1 className="h1">{selectedState.name}</h1>
            <div className="stats">
              <Stat value={Math.round(selectedState.totalKm).toLocaleString()} unit="km" label="Lines" />
              <Stat value={Math.round(selectedState.forestKm).toLocaleString()} unit="km" label="In forest" />
              <Stat value={String(selectedState.providers.length)} label="Operators" />
            </div>
            <button type="button" className="btn btn-primary" disabled={opening} aria-busy={opening} onClick={() => void enterSelectedState()}>
              {opening ? 'Opening' : 'Open'}
            </button>
            {loadError && (
              <p className="notice">
                <TriangleAlert size={14} /> Backend unreachable
              </p>
            )}
          </div>
        )}

        {stage === 'state' && selectedState && (
          <>
            <div className="sidebar-head">
              <div className="sidebar-head-row">
                <button type="button" className="link" onClick={backToOverview}>
                  All states
                </button>
                <button type="button" className="icon-btn" onClick={() => setPanelOpen(false)} aria-label="Hide panel">
                  <X size={16} />
                </button>
              </div>
              <h1 className="h1">{selectedState.name}</h1>
            </div>

            <nav className="tabs" role="tablist">
              {(
                [
                  ['fires', 'Fires'],
                  ...(modelTab ? ([['model', modelTab]] as const) : []),
                  ['settings', 'Settings'],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={tab === id}
                  className={tab === id ? 'is-active' : ''}
                  onClick={() => openTab(id)}
                >
                  {label}
                  {id === 'fires' && situation && <span className="num">{fireCount}</span>}
                </button>
              ))}
            </nav>

            {tab === 'fires' && (
              <div className="sidebar-scroll" role="tabpanel" aria-label="Fires">
                {situation && (
                  <p className="summary">
                    <span>
                      Wind{' '}
                      <span className="num">
                        {Math.round(situation.weather.avgWindMph)} mph {situation.weather.dominantWindLabel}
                      </span>
                    </span>
                    <span>
                      Humidity <span className="num">{situation.weather.minRhPct}%</span>
                    </span>
                    {loading && <Loader2 size={12} className="spin" aria-label="Updating" />}
                  </p>
                )}
                {loadError && (
                  <p className="notice">
                    <TriangleAlert size={14} /> Backend unreachable
                    <button type="button" className="link" onClick={() => setRetryTick((t) => t + 1)}>
                      Retry
                    </button>
                  </p>
                )}
                {!situation ? (
                  !loadError && <p className="empty">Loading fires</p>
                ) : visibleFires.length === 0 ? (
                  <p className="empty">{forestOnly ? 'No active forest fires' : 'No active fires'}</p>
                ) : (
                  visibleFires.map((cl) => {
                    const w = windByCluster.get(cl.id);
                    const coords = cl.name.startsWith('Fire near ');
                    return (
                      <button
                        key={cl.id}
                        type="button"
                        className={`item item-2 ${cl.id === focusedClusterId ? 'is-active' : ''}`}
                        onClick={() => flyToCluster(cl)}
                      >
                        <span className="item-row">
                          <span className={`item-main ${coords ? 'num' : ''}`} title={cl.name}>
                            <i
                              className={`fire-dot fire-dot-${cl.sizeClass ?? 'small'}`}
                              title={FIRE_SIZE_STYLE[cl.sizeClass ?? 'small'].label}
                            />
                            {coords ? cl.name.slice('Fire near '.length) : cl.name}
                          </span>
                          <span className="item-side">
                            {cl.areaAcres.toLocaleString()} <span className="unit">ac</span>
                          </span>
                        </span>
                        <span className="item-row item-sub">
                          <span>
                            {cl.forestFraction !== null ? `${Math.round(cl.forestFraction * 100)}% forest, ` : ''}
                            {fmtAge(cl.minHoursAgo)} ago
                          </span>
                          {w && (
                            <span className="wind" title={`Wind from ${w.fromLabel}, pushing the fire toward ${w.bearingDeg}°`}>
                              <ArrowUp size={12} style={{ transform: `rotate(${w.bearingDeg}deg)` }} />
                              {Math.round(w.mph)} mph
                            </span>
                          )}
                        </span>
                      </button>
                    );
                  })
                )}
                {situation && (
                  <button type="button" className="link link-block" onClick={() => setForestOnly((v) => !v)}>
                    {forestOnly
                      ? droppedDetections > 0
                        ? `${droppedDetections.toLocaleString()} non-forest detections hidden. Show`
                        : 'Forest fires only. Show all'
                      : 'All detections shown. Forest only'}
                  </button>
                )}
              </div>
            )}

            {tab === 'model' && burnMeta && (
              <div className="sidebar-scroll sidebar-pad" role="tabpanel" aria-label="Model map">
                <div className="block">
                  {hasNisar && nisar ? (
                    <>
                      <span className="label">NISAR L-band</span>
                      <div className="stats">
                        {nisar.sme2 > 0 && (
                          <Stat value={String(nisar.sme2)} label="SME2 scenes" title="NISAR SME2 soil-moisture scenes behind this result" />
                        )}
                        {burnMeta.nisar_share != null && (
                          <Stat
                            value={String(Math.round(burnMeta.nisar_share * 100))}
                            unit="%"
                            label="of cells"
                            title="Share of cells that received the NISAR SME2 soil-moisture term"
                          />
                        )}
                        {nisar.gcov > 0 && (
                          <Stat value={String(nisar.gcov)} label="GCOV scenes" title="NISAR GCOV backscatter scenes behind this result" />
                        )}
                      </div>
                    </>
                  ) : (
                    burnMeta.synthetic && (
                      <span className="tag">Synthetic demo</span>
                    )
                  )}
                  {(burnMeta.valid_from || burnMeta.model_version) && (
                    <dl className="kv kv-facts">
                      {burnMeta.valid_from && (
                        <>
                          <dt>Map day</dt>
                          <dd>{burnMeta.valid_from.slice(0, 10)}</dd>
                        </>
                      )}
                      {burnMeta.model_version && (
                        <>
                          <dt>Model</dt>
                          <dd title={burnMeta.model_version}>{burnMeta.model_version}</dd>
                        </>
                      )}
                    </dl>
                  )}
                  <Check
                    label="Show on map"
                    checked={showBurnRaster}
                    onChange={(v) => (v ? flyToModelArea() : setShowBurnRaster(false))}
                  />
                  <button type="button" className="link" onClick={flyToModelArea}>
                    Go to model area
                  </button>
                </div>

                <div className="block">
                  <span className="label">Layer</span>
                  {burnGroups.length > 1 && (
                    <div className="btn-group is-fill">
                      {burnGroups.map((g) => (
                        <button
                          key={g.label}
                          type="button"
                          className={g === burnGroup ? 'is-active' : ''}
                          aria-pressed={g === burnGroup}
                          onClick={() => g !== burnGroup && setBurnLayerId(g.layers[0].id)}
                        >
                          {g.label}
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="layer-list" role="radiogroup" aria-label="Layer">
                    {burnGroup?.layers.map((l) => (
                      <button
                        key={l.id}
                        type="button"
                        role="radio"
                        aria-checked={l.id === burnLayerId}
                        className={`item ${l.id === burnLayerId ? 'is-active' : ''}`}
                        title={layerHint(l.id, burnMeta) || l.title}
                        onClick={() => setBurnLayerId(l.id)}
                      >
                        <span className="item-main">{l.title}</span>
                        {layerHint(l.id, burnMeta).startsWith('Experimental') && <span className="unit">experimental</span>}
                      </button>
                    ))}
                  </div>
                  {burnLayer && (
                    <Legend layer={burnLayer} hint={layerHint(burnLayer.id, burnMeta)} value={pointSample?.values[burnLayerId]} />
                  )}
                  {showBurnRaster && (
                    <Check
                      label={burnMeta.synthetic ? 'Fuel pathways' : 'Spread vectors, 1 h'}
                      checked={showBurnVectors}
                      onChange={setShowBurnVectors}
                    />
                  )}
                </div>

                {showBurnRaster && (
                  <div className="block">
                    <div className="label-row">
                      <span className="label">Clicked cell</span>
                      {pointSample?.sampled_cell_center && (
                        <span className="num unit">
                          {pointSample.sampled_cell_center.lat.toFixed(3)}, {pointSample.sampled_cell_center.lon.toFixed(3)}
                        </span>
                      )}
                    </div>
                    {pointSample ? (
                      <PointValues sample={pointSample} layers={burnMeta.layers} selected={burnLayerId} />
                    ) : (
                      <div className="burn-legend-hint">Click the map to read a cell</div>
                    )}
                  </div>
                )}

                {showBurnRaster && burnMeta.capabilities?.spread && (
                  <div className="block" ref={growthBlockRef}>
                    <Check label="Fire growth" checked={showGrowth} onChange={setShowGrowth} />
                    {growthOn && (
                      <div className="growth">
                        <select
                          className="input"
                          aria-label="Wind"
                          value={growthCustomWind ? 'set' : 'map'}
                          onChange={(e) => setGrowthCustomWind(e.target.value === 'set')}
                        >
                          <option value="map">Wind of the map day</option>
                          <option value="set">Set wind</option>
                        </select>
                        {growthCustomWind && (
                          <div className="growth-wind">
                            <label>
                              <input
                                className="input"
                                type="number"
                                min={0}
                                max={40}
                                step={1}
                                aria-label="Wind speed, m/s"
                                aria-invalid={growthWind === undefined}
                                value={growthWindSpeed}
                                onChange={(e) => setGrowthWindSpeed(e.target.value)}
                              />
                              <span>m/s</span>
                            </label>
                            <label>
                              <input
                                className="input"
                                type="number"
                                min={0}
                                max={360}
                                step={5}
                                aria-label="Wind from, degrees"
                                aria-invalid={growthWind === undefined}
                                value={growthWindFrom}
                                onChange={(e) => setGrowthWindFrom(e.target.value)}
                              />
                              <span>° from</span>
                            </label>
                          </div>
                        )}
                        <Check label="Fuel age" checked={growthFuelAge} onChange={setGrowthFuelAge} />
                        {burnMeta.capabilities.spread_fuel_state && (
                          <Check label="Compare with fuel map alone" checked={growthCompare} onChange={setGrowthCompare} />
                        )}
                        {compareOn && (
                          <div className="btn-group is-fill" role="group" aria-label="Estimate drawn">
                            <button type="button" className={nisarShown ? 'is-active' : ''} aria-pressed={nisarShown} onClick={() => showNisar(true)}>
                              With NISAR
                            </button>
                            <button type="button" className={nisarShown ? '' : 'is-active'} aria-pressed={!nisarShown} onClick={() => showNisar(false)}>
                              Fuel map alone
                            </button>
                          </div>
                        )}
                        {!growthPoint && <div className="burn-legend-hint">Click the map to start a fire</div>}
                        {growthBusy && (
                          <div className="burn-legend-hint">
                            <Loader2 size={11} className="spin" /> Growing
                          </div>
                        )}
                        {growthShown?.error && (
                          <p className="notice">
                            <TriangleAlert size={13} /> {growthShown.error}
                          </p>
                        )}
                        {growthMain?.growth && <GrowthValues growth={growthMain.growth} />}
                        {compareOn && aloneShown?.error && (
                          <p className="notice">
                            <TriangleAlert size={13} /> {aloneShown.error}
                          </p>
                        )}
                        {growthPair && <GrowthCompare pair={growthPair} nisarShown={nisarShown} />}
                        <div
                          className="burn-legend-hint"
                          title="Surface-fire potential for one wind and the map day's fuels. No suppression."
                        >
                          Potential for one wind, not a forecast
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {tab === 'settings' && (
              <div className="sidebar-scroll sidebar-pad" role="tabpanel" aria-label="Settings">
                <div className="block">
                  <span className="label">Fires from last</span>
                  <div className="btn-group">
                    {[24, 72, 168].map((h) => (
                      <button
                        key={h}
                        type="button"
                        className={fireHours === h ? 'is-active' : ''}
                        aria-pressed={fireHours === h}
                        onClick={() => setFireHours(h)}
                      >
                        {h === 24 ? '24 h' : h === 72 ? '3 d' : '7 d'}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="block">
                  <span className="label">Safety buffer</span>
                  <div className="btn-group">
                    {[1000, 2000, 3000].map((r) => (
                      <button
                        key={r}
                        type="button"
                        className={radiusM === r ? 'is-active' : ''}
                        aria-pressed={radiusM === r}
                        onClick={() => setRadiusM(r)}
                      >
                        {r / 1000} km
                      </button>
                    ))}
                  </div>
                </div>

                <div className="block">
                  <span className="label">Map</span>
                  <Check label="Forest fires only" checked={forestOnly} onChange={setForestOnly} />
                  <Check label="Tree cover" checked={showCanopy} onChange={setShowCanopy} />
                  <Check label="All lines in forest" checked={showForestLines} onChange={setShowForestLines} />
                  {showForestLines && (
                    <div className="color-row">
                      <span>Forest line colour</span>
                      <div className="color-swatches">
                        {FOREST_PRESETS.map((c) => (
                          <button
                            key={c}
                            type="button"
                            className={`color-chip ${forestLineColor.toLowerCase() === c ? 'is-active' : ''}`}
                            style={{ background: c }}
                            aria-label={`Use ${c}`}
                            onClick={() => setForestLineColor(c)}
                          />
                        ))}
                        <input
                          type="color"
                          className="color-input"
                          aria-label="Custom forest line colour"
                          value={forestLineColor}
                          onChange={(e) => setForestLineColor(e.target.value)}
                        />
                      </div>
                    </div>
                  )}
                </div>

                <div className="block">
                  <div className="label-row">
                    <span className="label">
                      Operators{' '}
                      <span className="num">
                        {owners.size}/{selectedState.providers.length}
                      </span>
                    </span>
                    <span className="label-actions">
                      <button
                        type="button"
                        className="link"
                        onClick={() => setOwners(new Set(selectedState.providers.map((p) => p.owner)))}
                      >
                        All
                      </button>
                      <button type="button" className="link" onClick={() => setOwners(new Set())}>
                        None
                      </button>
                    </span>
                  </div>
                  {selectedState.providers.length > 8 && (
                    <input
                      type="text"
                      className="input"
                      placeholder="Filter operators"
                      aria-label="Filter operators"
                      value={providerSearch}
                      onChange={(e) => setProviderSearch(e.target.value)}
                    />
                  )}
                  <div className="checklist checklist-dense">
                    {filteredProviders.map((prov) => (
                      <Check
                        key={prov.owner}
                        label={ownerLabel(prov.owner)}
                        checked={owners.has(prov.owner)}
                        onChange={() => toggleOwner(prov.owner)}
                      />
                    ))}
                  </div>
                </div>
              </div>
            )}
            <div className="sidebar-foot">{sources}</div>
          </>
        )}
      </aside>

      {stage === 'state' && hoveredFire && hoverFire && (
        <FireCard
          cluster={hoveredFire}
          spread={spreadById.get(hoveredFire.id)}
          threat={threatByFire.get(hoveredFire.id)}
          style={{ transform: cardTransform(hoverFire.x, hoverFire.y) }}
        />
      )}
      {stage === 'state' && pinnedFire && (
        <FireCard
          key={pinnedFire.id}
          cluster={pinnedFire}
          spread={spreadById.get(pinnedFire.id)}
          threat={threatByFire.get(pinnedFire.id)}
          cardRef={pinnedCardRef}
          pinned
          onClose={() => setFocusedClusterId(null)}
        />
      )}

      {stage === 'state' && (
        <ReportsPanel
          situation={situation && situation.state === selectedAbbr ? situation : null}
          lines={lines}
          open={reportsOpen}
          onOpenChange={setReportsOpen}
          onFlyToLine={flyToLine}
          onCompanyChange={setCompanyLines}
        />
      )}

      <div ref={tooltipRef} className="tooltip" hidden>
        {hoveredState?.name}
      </div>

      {stage === 'state' && (
        <div className={`legend ${panelCollapsed ? 'is-wide' : ''}`} role="group" aria-label="Map legend">
          {showBurnRaster && !hideRaster && burnMeta && burnLayer && (
            <div className="legend-row">
              <MapModelLegend layer={burnLayer} meta={burnMeta} nisar={hasNisar} />
            </div>
          )}
          <div className="legend-row">
          <div className="legend-group">
            <span>
              <i className="swatch swatch-shutoff" /> Shut off
            </span>
            <span>
              <i className="swatch swatch-psps" /> PSPS
            </span>
          </div>
          <div className="legend-group">
            <span>
              <i className="fire-dot fire-dot-large" /> Large fire
            </span>
            <span>
              <i className="fire-dot fire-dot-medium" /> Medium
            </span>
            <span>
              <i className="fire-dot fire-dot-small" /> Small
            </span>
          </div>
          <div className="legend-group">
            <span>
              <i className="swatch swatch-spread" /> 24 h spread
            </span>
            <span>
              <i className="swatch swatch-wind" /> Wind
            </span>
          </div>
          </div>
        </div>
      )}

      <div className="toolbar">
        <div className="btn-group">
          {(['3d', '2d'] as const).map((m) => (
            <button
              key={m}
              type="button"
              className={viewMode === m ? 'is-active' : ''}
              aria-pressed={viewMode === m}
              onClick={() => setViewMode(m)}
            >
              {m.toUpperCase()}
            </button>
          ))}
        </div>
        <div className="btn-group">
          <button
            type="button"
            className={basemapMode === 'satellite' ? 'is-active' : ''}
            aria-pressed={basemapMode === 'satellite'}
            onClick={() => setBasemapMode('satellite')}
          >
            Satellite
          </button>
          <button
            type="button"
            className={basemapMode === 'map' ? 'is-active' : ''}
            aria-pressed={basemapMode === 'map'}
            onClick={() => setBasemapMode('map')}
          >
            Map
          </button>
        </div>
        <div className="btn-group">
          <button type="button" aria-label="Zoom in" onClick={() => stepZoom(1)}>
            <Plus size={14} />
          </button>
          <button type="button" aria-label="Zoom out" onClick={() => stepZoom(-1)}>
            <Minus size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

interface FireThreat {
  shutoff: number;
  shutoffKm: number;
  psps: number;
  pspsKm: number;
}

const CARD_W = 300;

/** Places a card beside a screen point, flipping to stay inside the viewport. */
function cardTransform(x: number, y: number, el?: HTMLElement | null): string {
  const h = el?.offsetHeight ?? 110;
  const w = el?.offsetWidth || CARD_W;
  const left = x + 18 + w > window.innerWidth - 8 ? x - 18 - w : x + 18;
  const top = Math.min(window.innerHeight - h - 8, Math.max(8, y - h / 2));
  return `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

function compactAcres(ac: number): string {
  return ac >= 10_000 ? `${Math.round(ac / 1000)}k` : ac.toLocaleString();
}

/** Age of the newest detection: "<1 h", "8 h", "3 d". */
function fmtAge(hours: number): string {
  return hours < 1 ? '<1 h' : hours < 48 ? `${Math.round(hours)} h` : `${Math.round(hours / 24)} d`;
}

const kmText = (km: number) => km.toFixed(km < 10 ? 1 : 0);

/** A figure with its unit over a short label. */
function Stat({ value, unit, label, title }: { value: string; unit?: string; label: React.ReactNode; title?: string }) {
  return (
    <div className="stat" title={title}>
      <span className="stat-fig">
        <b className="fig">{value}</b>
        {unit && <span className="unit">{unit}</span>}
      </span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function FireCard({
  cluster,
  spread,
  threat,
  pinned = false,
  onClose,
  cardRef,
  style,
}: {
  cluster: FireCluster;
  spread?: Situation['spread'][number];
  threat?: FireThreat;
  pinned?: boolean;
  onClose?: () => void;
  cardRef?: React.Ref<HTMLDivElement>;
  style?: React.CSSProperties;
}) {
  const size = cluster.sizeClass ?? 'small';
  const wind = spread?.wind;
  return (
    <div ref={cardRef} className={`fire-card ${pinned ? 'is-pinned' : ''}`} style={style}>
      <div className="fire-card-head">
        <span>{FIRE_SIZE_STYLE[size].label}</span>
        <span className="fire-card-age" title="Newest detection">
          <Clock size={12} /> {fmtAge(cluster.minHoursAgo)}
        </span>
        {pinned && onClose && (
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        )}
      </div>
      <div className="fire-card-stats">
        <div title={spread ? 'Burned now, and the forecast area in 24 hours' : 'Burned now'}>
          <b className="fig">{compactAcres(cluster.areaAcres)}</b>
          <span className="unit">ac</span>
          {spread && (
            <>
              <span className="unit t-arrow">to</span>
              <b className="fig t-forecast">{compactAcres(Math.round(spread.acres))}</b>
              <span className="unit">ac in 24 h</span>
            </>
          )}
        </div>
        {wind && (
          <div className="t-wind" title={`Wind from ${wind.fromLabel}`}>
            <ArrowUp size={14} style={{ transform: `rotate(${wind.bearingDeg}deg)` }} />
            <b className="fig t-wind">{Math.round(wind.mph)}</b>
            <span className="unit">mph</span>
          </div>
        )}
        {cluster.fuelRiskClass != null && (
          <div title="Fuel risk class at the fire, 1 to 5, from the model map">
            <b className="fig">{cluster.fuelRiskClass}</b>
            <span className="unit">/5 fuel</span>
          </div>
        )}
      </div>
      {threat && (threat.shutoff > 0 || threat.psps > 0) && (
        <div className="fire-card-threat tiers">
          {threat.shutoff > 0 && (
            <span className="tier" title="Lines to shut off now because of this fire">
              <i className="swatch swatch-shutoff" />
              <b>
                {threat.shutoff} {threat.shutoff === 1 ? 'line' : 'lines'}
              </b>
              {kmText(threat.shutoffKm)} km
            </span>
          )}
          {threat.psps > 0 && (
            <span className="tier" title="Lines to prepare for a shutoff (PSPS) because of this fire">
              <i className="swatch swatch-psps" />
              <b>
                {threat.psps} {threat.psps === 1 ? 'line' : 'lines'}
              </b>
              {kmText(threat.pspsKm)} km
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function fmtValue(v: number): string {
  const a = Math.abs(v);
  if (Number.isInteger(v)) return String(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
}

/** A ratio (units `x`), as "×2.4". */
function fmtFactor(v: number): string {
  return `${Number.isInteger(v) || v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : String(Number(v.toFixed(2)))}x`;
}

/** Ramp (or class swatches) of a result layer as a CSS gradient, with its two ends. */
function legendScale(layer: BurnabilityLayerMeta) {
  const stops = layer.legend?.stops ?? [];
  if (stops.length < 2) return null;
  const [lo, hi] = [stops[0][0], stops[stops.length - 1][0]];
  const background = layer.discrete
    ? `linear-gradient(to right, ${stops
        .map(([, c], i) => `${c} ${(i / stops.length) * 100}%, ${c} ${((i + 1) / stops.length) * 100}%`)
        .join(', ')})`
    : `linear-gradient(to right, ${stops.map(([v, c]) => `${c} ${((v - lo) / (hi - lo || 1)) * 100}%`).join(', ')})`;
  return { lo, hi, background };
}

/** Colour ramp (or class swatches) of a result layer with its range and units, as the backend defines them. */
function Legend({ layer, hint, value }: { layer: BurnabilityLayerMeta; hint: string; value?: number | null }) {
  const scale = legendScale(layer);
  if (!scale) return null;
  const { lo, hi, background } = scale;
  const factor = layer.units === 'x';
  const units = layer.units && layer.units !== '1' ? ` ${layer.units}` : '';
  const neutral = layer.neutral != null && layer.neutral > lo && layer.neutral < hi ? layer.neutral : null;
  const tick = value != null && hi > lo ? Math.min(100, Math.max(0, ((value - lo) / (hi - lo)) * 100)) : null;
  return (
    <div className="burn-legend">
      <div className="burn-legend-bar" style={{ background }}>
        {tick != null && <i className="burn-legend-tick" style={{ left: `${tick}%` }} title="Clicked cell" />}
      </div>
      <div className="burn-legend-ends">
        {layer.cyclic ? (
          ['N', 'E', 'S', 'W', 'N'].map((point, i) => <span key={i}>{point}</span>)
        ) : factor ? (
          <>
            <span>{fmtFactor(lo)}</span>
            {neutral != null && (
              <span className="burn-legend-mark" style={{ left: `${((neutral - lo) / (hi - lo)) * 100}%` }}>
                {fmtFactor(neutral)}
              </span>
            )}
            <span>{fmtFactor(hi)}</span>
          </>
        ) : (
          <>
            <span>{fmtValue(lo)}</span>
            <span>
              {fmtValue(hi)}
              {units}
            </span>
          </>
        )}
      </div>
      {hint && <div className="burn-legend-hint">{hint}</div>}
    </div>
  );
}

/** The model layer in the map legend: where it comes from, what it shows, its ramp. */
function MapModelLegend({ layer, meta, nisar }: { layer: BurnabilityLayerMeta; meta: BurnabilityMetadata; nisar: boolean }) {
  const scale = legendScale(layer);
  const factor = layer.units === 'x';
  const units = layer.units && layer.units !== '1' && !factor ? ` ${layer.units}` : '';
  const end = (v: number) => (factor ? fmtFactor(v) : fmtValue(v));
  return (
    <div className="legend-group legend-model">
      <span className="tag">
        {meta.synthetic ? 'Synthetic demo' : `${nisar ? 'NISAR' : 'Model'}${meta.valid_from ? ` ${meta.valid_from.slice(0, 10)}` : ''}`}
      </span>
      <span>{layer.title}</span>
      {scale && (
        <span>
          <span className="num">{layer.cyclic ? 'N' : end(scale.lo)}</span>
          <i className="legend-ramp" style={{ background: scale.background }} />
          <span className="num">
            {layer.cyclic ? 'N' : end(scale.hi)}
            {units}
          </span>
        </span>
      )}
    </div>
  );
}

/** Values of the clicked cell: the selected layer first, then the ones an operator reads. */
const POINT_LAYERS = [
  'risk_index',
  'risk_class',
  'susceptibility_index',
  'weather_factor',
  'lfmc',
  'lfmc_pct',
  'rate_of_spread',
  'flame_length',
  'fuel_state',
  'years_since_fire',
  'dryness_anomaly',
  'nisar_term',
];

function PointValues({
  sample,
  layers,
  selected,
}: {
  sample: BurnabilityPointResult;
  layers: BurnabilityLayerMeta[];
  selected: BurnabilityLayerId;
}) {
  const ids = [selected, ...POINT_LAYERS.filter((id) => id !== selected)].filter((id) => sample.values[id] != null);
  if (ids.length === 0) return <div className="burn-legend-hint">No values in this cell</div>;
  return (
    <dl className="kv">
      {ids.map((id) => {
        const meta = layers.find((l) => l.id === id);
        const units = sample.units?.[id] ?? meta?.units ?? '';
        const title = meta?.title ?? id;
        return (
          <Fragment key={id}>
            <dt className={id === selected ? 'is-selected' : ''} title={title}>
              {title}
            </dt>
            <dd className={id === selected ? 'is-selected' : ''}>
              {units === 'x' ? fmtFactor(sample.values[id] as number) : fmtValue(sample.values[id] as number)}
              {units && units !== '1' && units !== 'x' ? ` ${units}` : ''}
            </dd>
          </Fragment>
        );
      })}
    </dl>
  );
}

/** Area after each hour, then the head fire and the wind the run was made with. */
/** Areas after the last hour both fires reached: with NISAR's fuel state and on the fuel map alone. */
function comparedAreas(
  growth: FireGrowth | null | undefined,
  alone: FireGrowth | null | undefined,
): { hours: number; nisar: number; alone: number } | null {
  if (!growth || !alone) return null;
  const area = (g: FireGrowth) => new Map(g.features.map((f) => [f.properties.hours, f.properties.area_ha]));
  const [a, b] = [area(growth), area(alone)];
  const hours = [...GROWTH_HOURS].reverse().find((h) => a.has(h) && b.has(h));
  return hours === undefined ? null : { hours, nisar: a.get(hours) as number, alone: b.get(hours) as number };
}

/** One line: how the estimate shown compares with the other one. */
function comparedText(pair: { nisar: number; alone: number }, nisarShown: boolean): string {
  const [shown, other] = nisarShown ? [pair.nisar, pair.alone] : [pair.alone, pair.nisar];
  const tail = `${fmtValue(other)} ha ${nisarShown ? 'without' : 'with'}`;
  if (!(shown > 0 && other > 0) || Math.abs(shown / other - 1) < 0.05) return `the same ${nisarShown ? 'without' : 'with'} NISAR`;
  const times = shown < other ? `${(other / shown).toFixed(1)}x smaller` : `${(shown / other).toFixed(1)}x larger`;
  return `${times} ${nisarShown ? 'with' : 'without'} NISAR, ${tail}`;
}

function GrowthCompare({ pair, nisarShown }: { pair: { hours: number; nisar: number; alone: number }; nisarShown: boolean }) {
  const ratio = pair.nisar > 0 && pair.alone > 0 ? pair.alone / pair.nisar : null;
  // the estimate drawn as the fire has the solid line, the other one the dashed outline
  const line = (drawn: boolean) => (
    <i
      className={`growth-line ${drawn ? '' : 'is-dashed'}`}
      style={{ borderColor: drawn ? growthColor(pair.hours) : GROWTH_ALONE_COLOR }}
    />
  );
  return (
    <dl className="kv growth-compare">
      <dt>
        {line(nisarShown)}
        {pair.hours} h, NISAR fuel state
      </dt>
      <dd>{fmtValue(pair.nisar)} ha</dd>
      <dt>
        {line(!nisarShown)}
        {pair.hours} h, fuel map alone
      </dt>
      <dd>{fmtValue(pair.alone)} ha</dd>
      {ratio !== null && Math.abs(ratio - 1) >= 0.05 && (
        <>
          <dt>With NISAR</dt>
          <dd>{ratio > 1 ? `${ratio.toFixed(1)}x smaller` : `${(1 / ratio).toFixed(1)}x larger`}</dd>
        </>
      )}
    </dl>
  );
}

function GrowthValues({ growth }: { growth: FireGrowth }) {
  const p = growth.properties;
  const area = new Map(growth.features.map((f) => [f.properties.hours, f.properties.area_ha]));
  const num = (v: number | null, digits: number) => (v == null ? 'n/a' : v.toFixed(digits));
  return (
    <dl className="kv">
      {GROWTH_HOURS.filter((h) => area.has(h)).map((h) => (
        <Fragment key={h}>
          <dt>
            <i className="growth-swatch" style={{ background: growthColor(h) }} />
            {h} h
          </dt>
          <dd title={p.edge_reached ? 'The fire reached the edge of the simulated window' : undefined}>
            {p.edge_reached && h === GROWTH_HOURS[GROWTH_HOURS.length - 1] ? 'at least ' : ''}
            {fmtValue(area.get(h) as number)} ha
          </dd>
        </Fragment>
      ))}
      <dt>Head fire</dt>
      <dd>
        {num(p.head_rate_m_min, 1)} m/min, {num(p.head_direction_deg, 0)}°
      </dd>
      <dt>Flame</dt>
      <dd>{num(p.flame_length_m, 1)} m</dd>
      <dt>{p.wind_source === 'map_day' ? 'Wind, map day' : 'Wind'}</dt>
      <dd>
        {num(p.wind_10m_m_s, 1)} m/s from {num(p.wind_from_deg, 0)}°
      </dd>
    </dl>
  );
}

function Check({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="check" title={label}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

export default App;
