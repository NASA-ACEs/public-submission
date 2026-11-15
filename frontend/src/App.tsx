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
import { ArrowLeft, ChevronDown, List, Settings, X } from 'lucide-react';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import './App.css';
import { ReportsPanel } from './report/ReportsPanel';
import { createLineRiskLayer, LINE_RISK_STYLE, loadLineRisk, type LineRiskSummary } from './grid/lineRisk';
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
import { DEMO } from './demo';
import { compass16, mercatorToLonLat, type BasemapMode, type ViewMode } from './grid/geo';
import { describeLocation, statePlaces } from './grid/places';
import { makeFmt, readUnits, saveUnits, type Fmt, type Units } from './grid/units';
import { SPREAD_DEFAULT, SPREAD_STEPS, spreadCss, spreadFires } from './grid/spreadHours';
import { indexLines, pickLine, type LineIndexEntry } from './grid/linePick';
import {
  applyAppearance,
  clearSituation,
  createLayers,
  filterOwners,
  type MapLayers,
  orderedLayers,
  lineGraphic,
  lineZoomTier,
  renderLineCasing,
  renderLineHover,
  renderFocus,
  renderFireFocus,
  renderCompanyFocus,
  companyExtent,
  renderForestLines,
  renderSituation,
  renderSpread,
  FIRE_SIZE_STYLE,
  setLines,
  setStateLayersVisible,
  setStates,
} from './grid/layers';
import { createBasemap, createForestCoverLayer, createGridMap } from './grid/powerGrid';

type AppStage = 'overview' | 'state';
type StateTab = 'fires' | 'settings';
type FireHover = { id: string; x: number; y: number };

// Floating panel: 12px gap + 280px card + 12px gap.
const SIDEBAR_W = 304;
const FOREST_COLOR_KEY = 'forestLineColor';
const DEFAULT_FOREST_COLOR = '#15803d';
const FOREST_PRESETS = ['#15803d', '#22c55e', '#065f46', '#a3e635', '#0ea5e9'];
const SIZE_ORDER = { large: 0, medium: 1, small: 2 } as const;
const MAP_PADDING = { left: SIDEBAR_W, top: 16, right: 16, bottom: 56 };
const REPORTS_W = 352; // 12px gap + 328px panel + 12px gap

interface ViewBundle {
  view: MapView | SceneView;
  map: ArcGISMap;
  forestLayer: WebTileLayer;
  layers: MapLayers;
  burnVectors: GraphicsLayer;
  fireGrowth: GraphicsLayer;
  lineRisk: GraphicsLayer;
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

// coverRight: px covered by a panel on the right; the target is framed left of it (view.padding stays fixed so the map never jumps).
function flyTo(view: MapView | SceneView | undefined, target: Extent, tilt: number, duration = 600, coverRight = 0) {
  if (!view || view.destroyed) return;
  const free = view.width - view.padding.left - view.padding.right - coverRight;
  if (coverRight > 0 && free > 80) {
    target = target.clone();
    target.xmax += (target.width * coverRight) / free;
  }
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
  const [viewMode, setViewMode] = useState<ViewMode>('3d');
  const [basemapMode, setBasemapMode] = useState<BasemapMode>('satellite');
  const [readyTick, setReadyTick] = useState(0);
  // Spread forecast hours shown on the map (each its own colour); toggled in Settings.
  const [spreadHours, setSpreadHours] = useState<number[]>(SPREAD_DEFAULT);
  const spreadHoursRef = useRef<number[]>(SPREAD_DEFAULT);
  // Display units (data stays in backend units); remembered per browser.
  const [units, setUnitsState] = useState<Units>(readUnits);
  const fmt = useMemo(() => makeFmt(units), [units]);
  const fmtRef = useRef(fmt);
  fmtRef.current = fmt;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const [legendCollapsed, setLegendCollapsed] = useState(() => {
    try {
      return localStorage.getItem('legendCollapsed') === '1';
    } catch {
      return false;
    }
  });
  const setUnits = (u: Units) => {
    if (u === units) return;
    // Keep the typed growth wind the same physical speed.
    const v = Number(growthWindSpeed);
    if (growthWindSpeed !== '' && Number.isFinite(v)) {
      const ms = fmt.windInput.toMs(v);
      setGrowthWindSpeed(String(Math.round(ms / makeFmt(u).windInput.toMs(1))));
    }
    setUnitsState(u);
    saveUnits(u);
  };
  useEffect(() => {
    if (!settingsOpen) return;
    const down = (e: PointerEvent) => {
      if (!settingsRef.current?.contains(e.target as Node)) setSettingsOpen(false);
    };
    const key = (e: KeyboardEvent) => e.key === 'Escape' && setSettingsOpen(false);
    document.addEventListener('pointerdown', down);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('pointerdown', down);
      document.removeEventListener('keydown', key);
    };
  }, [settingsOpen]);
  const toggleLegend = () => {
    const next = !legendCollapsed;
    setLegendCollapsed(next);
    try {
      localStorage.setItem('legendCollapsed', next ? '1' : '0');
    } catch {
      /* storage unavailable */
    }
  };

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
  // Power grid on the map (lines, shutoff/PSPS sections); off = fires only.
  const [showGrid, setShowGrid] = useState(true);
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
  const [showCanopy, setShowCanopy] = useState(true);
  const [tab, setTab] = useState<StateTab>('fires');
  const [reportsOpen, setReportsOpen] = useState(false);
  const [companyLines, setCompanyLines] = useState<ThreatLine[] | null>(null);
  const [hoverFire, setHoverFire] = useState<FireHover | null>(null);
  const fireHoverRef = useRef<(h: FireHover | null) => void>(() => {});
  const fireClickRef = useRef<(id: string | null) => void>(() => {});
  const growthClickRef = useRef<(lon: number, lat: number) => void>(() => {});
  const pinnedCardRef = useRef<HTMLDivElement | null>(null);
  const [forestOnly, setForestOnly] = useState(true);

  const [fireHours, setFireHours] = useState(72);
  const [radiusM, setRadiusM] = useState(3000);
  const [situation, setSituation] = useState<Situation | null>(null);
  const [places, setPlaces] = useState<Awaited<ReturnType<typeof statePlaces>>>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [focusedClusterId, setFocusedClusterId] = useState<string | null>(null);

  const [burnMeta, setBurnMeta] = useState<BurnabilityMetadata | null>(null);
  const [burnLayerId, setBurnLayerId] = useState<BurnabilityLayerId>('susceptibility_index');
  const [showBurnRaster, setShowBurnRaster] = useState(false);
  const [showBurnVectors, setShowBurnVectors] = useState(false);
  // Transmission lines coloured by large-fire risk class, every 200 m.
  const [showLineRisk, setShowLineRisk] = useState(false);
  const [lineRiskSum, setLineRiskSum] = useState<LineRiskSummary | null>(null);
  const [pointSample, setPointSample] = useState<BurnabilityPointResult | null>(null);
  // Fire growth from a clicked point (model results that have the layers for it).
  const [showGrowth, setShowGrowth] = useState(false);
  const [growthPoint, setGrowthPoint] = useState<{ lon: number; lat: number } | null>(null);
  const [growthAnswer, setGrowthAnswer] = useState<{ key: string; growth: FireGrowth | null; error: string | null } | null>(null);
  const [growthCustomWind, setGrowthCustomWind] = useState(false);
  // Typed in the display unit (mph or km/h); sent as m/s.
  const [growthWindSpeed, setGrowthWindSpeed] = useState(() => (readUnits() === 'imperial' ? '22' : '36'));
  const [growthWindFrom, setGrowthWindFrom] = useState('45');
  const [growthFuelAge, setGrowthFuelAge] = useState(true);

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
  const linesRef = useRef(lines);
  const lineIndexRef = useRef<LineIndexEntry[]>([]);
  const [lineTier, setLineTier] = useState<0 | 1 | 2>(0);
  const basemapRef = useRef(basemapMode);
  const burnMetaRef = useRef(burnMeta);
  const hoveredRef = useRef(hoveredAbbr);
  const selectedRef = useRef(selectedAbbr);
  useEffect(() => {
    stageRef.current = stage;
    statesRef.current = states;
    linesRef.current = lines;
    basemapRef.current = basemapMode;
    burnMetaRef.current = burnMeta;
    hoveredRef.current = hoveredAbbr;
    selectedRef.current = selectedAbbr;
  }, [stage, states, lines, basemapMode, burnMeta, hoveredAbbr, selectedAbbr]);

  // Picking a state opens it straight away (no intermediate summary screen).
  const pickState = useCallback(async (st: StateSummary) => {
    setLoadError(null);
    let stateLines: Awaited<ReturnType<typeof fetchStateLines>>;
    try {
      stateLines = await fetchStateLines(st.abbr);
    } catch (err) {
      setLoadError(String(err));
      return;
    }
    setSelectedAbbr(st.abbr);
    setTypedPreviewAbbr(null);
    setOwners(new Set(st.providers.map((p) => p.owner)));
    setProviderSearch('');
    setLinesList(stateLines);
    setSituation(null);
    setStage('state');
    setTab('fires');
    setFocusedClusterId(null);
    setHoveredAbbr(null);
    setShowBurnRaster(false);
    if (tooltipRef.current) tooltipRef.current.hidden = true;
    flyTo(bundleRef.current?.view, mercExtent(st.extent).expand(1.12), 16, 650);
  }, []);

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
      const lineRisk = createLineRiskLayer();
      const map = createGridMap(basemapRef.current, [forestLayer, ...orderedLayers(layers), lineRisk, burnVectors, fireGrowth]);
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

      const bundle: ViewBundle = { view, map, forestLayer, layers, burnVectors, fireGrowth, lineRisk, burnRaster: null };
      let active = true;
      let hitPending = false;
      let lastEvent: { x: number; y: number } | null = null;
      let hoveredLineOid: number | null = null;

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
                const fire = hit.results.find((r): r is { graphic: Graphic } & typeof r => 'graphic' in r)?.graphic;
                const id = fire?.attributes?.CID ? String(fire.attributes.CID) : null;
                // Lines by distance (pickLine), not hitTest: 3D hitTest misses thin draped lines.
                // Highlighted even when a fire is hit too; its card still shows.
                const line = pickLine(view, lineIndexRef.current, ev);
                const oid = line?.oid ?? null;
                if (oid !== hoveredLineOid) {
                  hoveredLineOid = oid;
                  renderLineHover(layers, line?.paths ?? null, basemapRef.current);
                }
                container.style.cursor = id || oid != null ? 'pointer' : '';
                fireHoverRef.current(id ? { id, x: ev.x, y: ev.y } : null);
              })
              .catch(() => {})
              .finally(() => {
                hitPending = false;
              });
          });
          return;
        }
        hoveredLineOid = null; // line hover graphic is cleared by setLines on the next state
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
        hoveredLineOid = null;
        renderLineHover(layers, null, basemapRef.current);
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
        const line = pickLine(view, lineIndexRef.current, event);
        if (line) void view.openPopup({ features: [lineGraphic(line, fmtRef.current)], location: event.mapPoint ?? undefined });
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

      // Grid style follows the zoom (see lineZoomTier); only tier changes re-render.
      const scaleHandle = reactiveUtils.watch(
        () => view.scale,
        (scale) => setLineTier(lineZoomTier(scale)),
        { initial: true },
      );

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
        scaleHandle.remove();
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
    applyAppearance(b.layers, basemapMode, lineTier);
    b.forestLayer.opacity = basemapMode === 'map' ? 0.22 : 0.3;
    b.forestLayer.visible = inState && showCanopy;
    b.burnVectors.visible = inState && showBurnVectors;
    b.lineRisk.visible = inState && showLineRisk;
    setStateLayersVisible(b.layers, inState, showForestLines, showGrid);
  }, [readyTick, basemapMode, lineTier, stage, showCanopy, showForestLines, showGrid, showBurnVectors, showLineRisk]);

  // The pieces of line by risk class are loaded once per view, when first shown.
  useEffect(() => {
    const b = bundleRef.current;
    if (!b || !showLineRisk || b.lineRisk.graphics.length) return;
    const ac = new AbortController();
    loadLineRisk(b.lineRisk, ac.signal)
      .then(setLineRiskSum)
      .catch((e) => {
        if (!ac.signal.aborted) console.warn(e);
      });
    return () => ac.abort();
  }, [readyTick, showLineRisk]);

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
    // Hover and click pick only lines that are drawn.
    lineIndexRef.current = showGrid ? indexLines(lines.filter((l) => owners.has(l.owner))) : [];
    const b = bundleRef.current;
    if (b && stage === 'state') renderLineCasing(b.layers, lines, owners);
  }, [readyTick, stage, lines, owners, showGrid]);

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
  }, [stage, selectedState, fireHours, radiusM, ownersKey, forestOnly]);

  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    if (stage === 'state' && situation && situation.state === selectedAbbr) {
      renderSituation(b.layers, situation, basemapMode, spreadHoursRef.current, fmt);
    } else {
      clearSituation(b.layers);
    }
  }, [readyTick, stage, situation, selectedAbbr, basemapMode, fmt]);

  // Forecast hours toggled: rebuild only the spread layer (the effect above covers new situations).
  useEffect(() => {
    if (spreadHoursRef.current === spreadHours) return;
    spreadHoursRef.current = spreadHours;
    const b = bundleRef.current;
    if (b && stage === 'state' && situation && situation.state === selectedAbbr) {
      renderSpread(b.layers, situation, basemapMode, spreadHours);
    }
  }, [spreadHours, stage, situation, selectedAbbr, basemapMode]);

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
  const reportsCover = stage === 'state' && reportsOpen ? REPORTS_W - MAP_PADDING.right : 0;
  useEffect(() => {
    if (!situation || !companyLines?.length) return;
    const ext = companyExtent(situation, companyLines);
    if (ext) flyTo(bundleRef.current?.view, mercExtent(ext).expand(1.25), 20, 700, reportsCover);
    // Frame once per company, not on every situation refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyOwner]);

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
    if (!b || stage !== 'state' || !showBurnRaster || !burnMeta) return;
    const overlay = createBurnabilityRasterLayer(burnMeta, burnLayerId, burnLayerId === 'fuel_state' ? 0.34 : 0.72);
    b.burnRaster = overlay;
    b.map.add(overlay, Math.max(0, b.map.layers.indexOf(b.layers.lines)));
    return () => {
      if (!b.map.destroyed) b.map.remove(overlay);
      overlay.destroy();
      if (b.burnRaster === overlay) b.burnRaster = null;
    };
  }, [readyTick, stage, showBurnRaster, burnLayerId, burnMeta]);

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
      void loadBurnabilityVectors(b.burnVectors, burnMeta, b.view.zoom ?? 10, fmt, ac.signal, box);
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
  }, [readyTick, stage, showBurnVectors, burnMeta, fmt]);

  // Fire growth: ask the backend when the point or an option changes, draw what it answers.
  const growthOn = showBurnRaster && showGrowth && !!burnMeta?.capabilities?.spread;
  const growthWind = useMemo(() => {
    if (!growthCustomWind) return null;
    const [speed, from] = [Number(growthWindSpeed), Number(growthWindFrom)];
    const ok =
      growthWindSpeed !== '' && growthWindFrom !== '' && speed >= 0 && speed <= fmt.windInput.max && from >= 0 && from <= 360;
    return ok ? { speed: +fmt.windInput.toMs(speed).toFixed(2), from } : undefined;
  }, [growthCustomWind, growthWindSpeed, growthWindFrom, fmt]);
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

  // Demo scenario (?demo= or VITE_DEMO): once the map, the states and the scenario's result are there, open the
  // state, the model settings, the scenario's layer and view, and start its fire. Runs once; then the app is the
  // user's again.
  const demoRanRef = useRef(false);
  useEffect(() => {
    const d = DEMO;
    if (!d || demoRanRef.current || !readyTick || burnMeta?.result_id !== d.result) return;
    const st = states.find((x) => x.abbr === d.state);
    if (!st) return;
    demoRanRef.current = true;
    void (async () => {
      await pickState(st);
      setTab('settings');
      setShowBurnRaster(true);
      setBurnLayerId(d.layer as BurnabilityLayerId);
      if (d.fire) {
        setShowGrowth(true);
        setGrowthCustomWind(true);
        setGrowthWindSpeed(String(+(d.fire.wind.speed / fmt.windInput.toMs(1)).toFixed(1)));
        setGrowthWindFrom(String(d.fire.wind.from));
        setGrowthPoint({ lon: d.fire.lon, lat: d.fire.lat });
      }
      await new Promise((done) => window.setTimeout(done, 700)); // after the state's own flight
      const view = bundleRef.current?.view;
      const target = d.camera && view instanceof SceneView ? d.camera : { center: d.center, zoom: d.zoom };
      if (view && !view.destroyed) void view.goTo(target, { duration: 900 }).catch(() => {});
    })();
  }, [readyTick, burnMeta, states, pickState, fmt]);

  // The same fire from the fuel map alone, without NISAR's fuel state, where the result offers it: drawn as a
  // dashed outline of its last hour next to the filled zones, and its area beside the other.
  const compareOn = !!burnMeta?.capabilities?.spread_fuel_state;
  const [growthAlone, setGrowthAlone] = useState<{ key: string; growth: FireGrowth | null } | null>(null);
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
      if (!ac.signal.aborted) setGrowthAlone({ key: growthKey, growth: r.growth });
    });
    return () => ac.abort();
  }, [compareOn, growthKey, burnMeta, growthPoint, growthWind, growthFuelAge]);
  const aloneShown = compareOn && growthAlone?.key === growthKey ? growthAlone.growth : null;

  useEffect(() => {
    const b = bundleRef.current;
    if (!b) return;
    const on = stage === 'state' && growthOn;
    b.fireGrowth.visible = on;
    drawFireGrowth(
      b.fireGrowth,
      on ? (growthShown?.growth ?? null) : null,
      on ? growthPoint : null,
      fmt,
      on ? aloneShown : null,
      on ? lines.flatMap((l) => l.paths) : null, // the section of a line inside the burned area turns red
    );
  }, [readyTick, stage, growthOn, growthShown, growthPoint, fmt, aloneShown, lines]);

  const backToOverview = useCallback(() => {
    setStage('overview');
    setSelectedAbbr(null);
    setTypedPreviewAbbr(null);
    setStateSearch('');
    setSearchCursorIdx(0);
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

  // Census places of the open state, for "4 mi NE of Anza" fire names.
  const placesState = stage === 'state' ? selectedAbbr : null;
  useEffect(() => {
    if (!placesState) return;
    let live = true;
    void statePlaces(placesState).then((p) => live && setPlaces(p));
    return () => {
      live = false;
    };
  }, [placesState]);

  // Named incidents (NIFC) keep their name; the rest are named by the nearest town.
  const fireLabels = useMemo(() => {
    const m = new Map<string, string>();
    for (const cl of situation?.fires.clusters ?? []) {
      let label = cl.name;
      if (cl.name.startsWith('Fire near')) {
        const [lon, lat] = mercatorToLonLat(cl.centroid[0], cl.centroid[1]);
        label = describeLocation(places, lon, lat, fmt) ?? cl.name;
      }
      m.set(cl.id, label);
    }
    return m;
  }, [situation, places, fmt]);

  // Threatened lines per fire (each line is linked to its nearest fire by the backend).
  const dangerById = useMemo(() => {
    const m = new Map<string, { shutoff: number; psps: number }>();
    for (const l of situation?.threats.lines ?? []) {
      const id = l.fire?.id;
      if (!id) continue;
      const d = m.get(id) ?? { shutoff: 0, psps: 0 };
      if (l.tier === 'SHUT OFF') d.shutoff += 1;
      else d.psps += 1;
      m.set(id, d);
    }
    return m;
  }, [situation]);

  // Most dangerous for the grid first: SHUT OFF lines, then PSPS lines, then fire size.
  // Fires that threaten no line go to the bottom.
  // Head distance (km from ignition) per forecast hour: the pinned fire, else the furthest of all fires.
  const reachKm = useMemo(() => {
    const m = new Map<number, number>();
    if (!situation) return m;
    const fires = spreadFires(situation).filter((f) => !focusedClusterId || f.clusterId === focusedClusterId);
    for (const h of SPREAD_STEPS) {
      let best: number | null = null;
      for (const f of fires) {
        const d = (f.hourly.off[h] ?? NaN) + (f.hourly.a[h] ?? NaN);
        if (Number.isFinite(d) && (best == null || d > best)) best = d;
      }
      if (best != null) m.set(h, best / 1000);
    }
    return m;
  }, [situation, focusedClusterId]);

  const sortedFires = useMemo(() => {
    const none = { shutoff: 0, psps: 0 };
    return [...(situation?.fires.clusters ?? [])].sort((a, b) => {
      const da = dangerById.get(a.id) ?? none;
      const db = dangerById.get(b.id) ?? none;
      return (
        Number(db.shutoff + db.psps > 0) - Number(da.shutoff + da.psps > 0) ||
        db.shutoff - da.shutoff ||
        db.psps - da.psps ||
        SIZE_ORDER[a.sizeClass ?? 'small'] - SIZE_ORDER[b.sizeClass ?? 'small'] ||
        b.areaAcres - a.areaAcres
      );
    });
  }, [situation, dangerById]);
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
    flyTo(bundleRef.current?.view, mercExtent([x0 - 6500, y0 - 6500, x1 + 6500, y1 + 6500]), 20, 600, reportsCover);
  };


  const flyToLine = (line: ThreatLine) => {
    flyTo(bundleRef.current?.view, mercExtent(line.bbox).expand(1.4), 20, 600, reportsCover);
  };

  const flyToModelArea = () => {
    if (!burnMeta) return;
    setShowBurnRaster(true);
    flyTo(bundleRef.current?.view, getResultExtentMercator(burnMeta).expand(1.2), 22, 650, reportsCover);
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

  return (
    <div className="app-shell">
      <div ref={mapHostRef} className="map-canvas" />

      <aside className="sidebar">
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
                <p className="empty">Can’t reach the backend. {statesError}</p>
              ) : states.length === 0 ? (
                <p className="empty">Loading states</p>
              ) : filteredStates.length === 0 ? (
                <p className="empty">No matching states.</p>
              ) : (
                filteredStates.map((st, idx) => {
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
                    </button>
                  );
                })
              )}
              {loadError && <p className="muted">{loadError}</p>}
            </div>
          </>
        )}

        {stage === 'state' && selectedState && (
          <>
            <div className="sidebar-head">
              <div className="sidebar-head-row">
                <button type="button" className="link" onClick={backToOverview}>
                  <ArrowLeft size={14} /> All states
                </button>
              </div>
              <h1 className="h1">{selectedState.name}</h1>
            </div>

            <nav className="tabs" role="tablist">
              {(
                [
                  ['fires', 'Fires'],
                  ['settings', 'Settings'],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={tab === id}
                  className={tab === id ? 'is-active' : ''}
                  onClick={() => setTab(id)}
                >
                  {label}
                </button>
              ))}
            </nav>

            {tab === 'fires' && (
              <div className="sidebar-scroll">
                {situation && loading && <p className="summary muted">Updating</p>}
                {!situation ? (
                  <p className="empty">Loading fires</p>
                ) : visibleFires.length === 0 ? (
                  <p className="empty">
                    {forestOnly ? 'No active forest fires.' : 'No active fires.'}
                  </p>
                ) : (
                  visibleFires.map((cl) => (
                    <button
                      key={cl.id}
                      type="button"
                      className={`item item-2 ${cl.id === focusedClusterId ? 'is-active' : ''}`}
                      onClick={() => flyToCluster(cl)}
                    >
                      <span className="item-row">
                        <span className="item-main">{fireLabels.get(cl.id) ?? cl.name}</span>
                        <span className="item-side">{fmt.acres(cl.areaAcres)}</span>
                      </span>
                      {(() => {
                        const d = dangerById.get(cl.id);
                        if (!d) return null;
                        return (
                          <span className="item-sub">
                            {d.shutoff > 0 && <span className="t-shutoff">{d.shutoff} shut off</span>}
                            {d.shutoff > 0 && d.psps > 0 && ', '}
                            {d.psps > 0 && <span className="t-psps">{d.psps} PSPS</span>}
                          </span>
                        );
                      })()}
                      <span className="item-sub">
                        {FIRE_SIZE_STYLE[cl.sizeClass ?? 'small'].label}
                        {cl.forestFraction !== null ? `, ${Math.round(cl.forestFraction * 100)}% forest` : ''}
                        {`, ${cl.minHoursAgo}h ago`}
                        {(() => {
                          const w = windByCluster.get(cl.id);
                          if (!w) return null;
                          const from = (Math.round(w.bearingDeg) + 180) % 360;
                          return `, wind ${compass16(from)} ${fmt.mph(w.mph)}`;
                        })()}
                      </span>
                    </button>
                  ))
                )}
                {situation && (
                  <button type="button" className="link link-block" onClick={() => setForestOnly((v) => !v)}>
                    {forestOnly
                      ? `Forest fires only${droppedDetections > 0 ? `, ${droppedDetections} non-forest detections hidden` : ''}. Show all`
                      : 'Showing all detections. Forest only'}
                  </button>
                )}
              </div>
            )}

            {tab === 'settings' && (
              <div className="sidebar-scroll sidebar-pad">
                <div className="block">
                  <span className="label">Fires from last</span>
                  <div className="btn-group">
                    {[24, 72, 168].map((h) => (
                      <button
                        key={h}
                        type="button"
                        className={fireHours === h ? 'is-active' : ''}
                        onClick={() => setFireHours(h)}
                      >
                        {h === 24 ? '24h' : h === 72 ? '3d' : '7d'}
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
                        onClick={() => setRadiusM(r)}
                      >
                        {fmt.dist(r)}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="block">
                  <div className="label-row">
                    <span className="label">Spread forecast</span>
                    <span className="label-note">{focusedClusterId ? 'this fire' : 'max reach'}</span>
                  </div>
                  <div className="checklist">
                    {SPREAD_STEPS.map((h) => {
                      const km = reachKm.get(h);
                      return (
                        <label key={h} className="check check-row">
                          <input
                            type="checkbox"
                            checked={spreadHours.includes(h)}
                            onChange={(e) =>
                              setSpreadHours((cur) =>
                                e.target.checked ? [...cur, h].sort((a, b) => a - b) : cur.filter((x) => x !== h),
                              )
                            }
                          />
                          <i className="swatch-hour" style={{ borderTopColor: spreadCss(h, basemapMode) }} aria-hidden />
                          <span>{h} h</span>
                          <span className="check-side">{fmt.km(km)}</span>
                        </label>
                      );
                    })}
                  </div>
                </div>

                <div className="block">
                  <span className="label">Map</span>
                  <Check label="Forest fires only" checked={forestOnly} onChange={setForestOnly} />
                  <Check label="Tree cover" checked={showCanopy} onChange={setShowCanopy} />
                  <Check label="Power grid" checked={showGrid} onChange={setShowGrid} />
                  {showGrid && (
                    <Check label="All lines in forest" checked={showForestLines} onChange={setShowForestLines} />
                  )}
                  {showGrid && showForestLines && (
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
                  {burnMeta && (
                    <>
                      <Check
                        label={burnMeta.synthetic ? 'Fuel risk' : 'Fuel risk (NISAR)'}
                        checked={showBurnRaster}
                        onChange={setShowBurnRaster}
                      />
                      {!burnMeta.synthetic && <Check label="Lines by large-fire risk" checked={showLineRisk} onChange={setShowLineRisk} />}
                      {showLineRisk && lineRiskSum && (
                        <div className="line-risk-key">
                          {(['c5', 'c4', 'low', 'none'] as const).map((k) => (
                            <span key={k}>
                              <i className="swatch" style={{ background: LINE_RISK_STYLE[k].color }} />
                              {LINE_RISK_STYLE[k].label}
                            </span>
                          ))}
                          <p className="muted">
                            {fmt.km(lineRiskSum.km_flagged)} of {fmt.km(lineRiskSum.km_covered)} of line in the top two risk classes,{' '}
                            {resultDay(lineRiskSum.result)}
                          </p>
                        </div>
                      )}
                      {showBurnRaster && (
                        <>
                          <Check
                            label={burnMeta.synthetic ? 'Fuel pathways' : 'Spread vectors, 1 h'}
                            checked={showBurnVectors}
                            onChange={setShowBurnVectors}
                          />
                          <select
                            className="input"
                            aria-label="Fuel layer"
                            value={burnLayerId}
                            onChange={(e) => setBurnLayerId(e.target.value as BurnabilityLayerId)}
                          >
                            {groupLayers(burnMeta.layers).map((g) => (
                              <optgroup key={g.label} label={g.label}>
                                {g.layers.map((l) => (
                                  <option key={l.id} value={l.id}>
                                    {l.title}
                                  </option>
                                ))}
                              </optgroup>
                            ))}
                          </select>
                          {burnLayer && <Legend fmt={fmt} layer={burnLayer} hint={layerHint(burnLayer.id, burnMeta, fmt)} />}
                          <button type="button" className="link" onClick={flyToModelArea}>
                            Go to model area
                          </button>
                          <dl className="kv kv-facts">
                            {burnMeta.synthetic && (
                              <>
                                <dt>Data</dt>
                                <dd>Synthetic demo</dd>
                              </>
                            )}
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
                            {nisar && burnMeta.nisar_share != null && (
                              <>
                                <dt>SME2</dt>
                                <dd>{nisar.sme2} scenes</dd>
                                <dt>SME2 cells</dt>
                                <dd>{Math.round(burnMeta.nisar_share * 100)}%</dd>
                              </>
                            )}
                            {nisar && nisar.gcov > 0 && (
                              <>
                                <dt>GCOV</dt>
                                <dd>{nisar.gcov} scenes</dd>
                              </>
                            )}
                          </dl>
                          {burnMeta.capabilities?.spread && (
                            <Check label="Fire growth" checked={showGrowth} onChange={setShowGrowth} />
                          )}
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
                                      max={fmt.windInput.max}
                                      step={1}
                                      aria-label={`Wind speed, ${fmt.windInput.unit}`}
                                      aria-invalid={growthWind === undefined}
                                      value={growthWindSpeed}
                                      onChange={(e) => setGrowthWindSpeed(e.target.value)}
                                    />
                                    <span>{fmt.windInput.unit}</span>
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
                              {!growthPoint && <div className="burn-legend-hint">Click the map to start a fire</div>}
                              {growthBusy && <div className="burn-legend-hint">Growing</div>}
                              {growthShown?.error && <div className="growth-error">{growthShown.error}</div>}
                              {growthShown?.growth && <GrowthValues growth={growthShown.growth} fmt={fmt} />}
                              {growthShown?.growth && aloneShown && (
                                <GrowthCompare nisar={growthShown.growth} alone={aloneShown} fmt={fmt} />
                              )}
                              <div className="burn-legend-hint">
                                Surface-fire potential for one wind and the map day's fuels. No suppression. Not a forecast.
                              </div>
                            </div>
                          )}
                          {pointSample && <PointValues fmt={fmt} sample={pointSample} layers={burnMeta.layers} selected={burnLayerId} />}
                        </>
                      )}
                    </>
                  )}
                </div>

                <div className="block">
                  <div className="label-row">
                    <span className="label">
                      Operators ({owners.size} of {selectedState.providers.length})
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
                  <div className="checklist">
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
          </>
        )}
      </aside>

      {stage === 'state' && hoveredFire && hoverFire && (
        <FireCard
          cluster={hoveredFire}
          label={fireLabels.get(hoveredFire.id)}
          spread={spreadById.get(hoveredFire.id)}
          fmt={fmt}
          style={{ transform: cardTransform(hoverFire.x, hoverFire.y) }}
        />
      )}
      {stage === 'state' && pinnedFire && (
        <FireCard
          key={pinnedFire.id}
          cluster={pinnedFire}
          label={fireLabels.get(pinnedFire.id)}
          spread={spreadById.get(pinnedFire.id)}
          fmt={fmt}
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
          fireLabel={(id) => fireLabels.get(id)}
          fmt={fmt}
        />
      )}

      <div ref={tooltipRef} className="tooltip" hidden>
        {hoveredState?.name}
      </div>

      {stage === 'state' && legendCollapsed && (
        <button
          type="button"
          className="legend legend-pill"
          aria-expanded={false}
          aria-label="Show legend"
          onClick={toggleLegend}
        >
          <List size={14} aria-hidden /> Legend
        </button>
      )}
      {stage === 'state' && !legendCollapsed && (
        <div className="legend" id="map-legend">
          <button
            type="button"
            className="legend-toggle"
            aria-expanded
            aria-controls="map-legend"
            aria-label="Hide legend"
            title="Hide legend"
            onClick={toggleLegend}
          >
            <ChevronDown size={14} aria-hidden />
          </button>
          <span>
            <i className="swatch swatch-shutoff" /> Shut off
          </span>
          <span>
            <i className="swatch swatch-psps" /> PSPS
          </span>
          <span>
            <i className="fire-dot fire-dot-large" /> Large fire
          </span>
          <span>
            <i className="fire-dot fire-dot-medium" /> Medium
          </span>
          <span>
            <i className="fire-dot fire-dot-small" /> Small
          </span>
          {spreadHours.map((h) => (
            <span key={h}>
              <i className="swatch-hour" style={{ borderTopColor: spreadCss(h, basemapMode) }} /> {h} h
            </span>
          ))}
          <span>
            <i className="swatch-wind">→</i> Wind
          </span>
        </div>
      )}

      <div className="toolbar">
        <div className="toolbar-settings" ref={settingsRef}>
          <div className="btn-group">
            <button
              type="button"
              className={settingsOpen ? 'is-active' : ''}
              aria-label="Settings"
              aria-expanded={settingsOpen}
              onClick={() => setSettingsOpen((o) => !o)}
            >
              <Settings size={15} />
            </button>
          </div>
          {settingsOpen && (
            <div className="settings-pop" role="dialog" aria-label="Settings">
              <span className="label">Units</span>
              <div className="btn-group">
                {(['imperial', 'metric'] as const).map((u) => (
                  <button key={u} type="button" className={units === u ? 'is-active' : ''} onClick={() => setUnits(u)}>
                    {u === 'imperial' ? 'Imperial' : 'Metric'}
                  </button>
                ))}
              </div>
              <span className="settings-note">{units === 'imperial' ? 'mi, ft, ac, mph' : 'km, m, ha, km/h'}</span>
            </div>
          )}
        </div>
        <div className="btn-group">
          {(['3d', '2d'] as const).map((m) => (
            <button
              key={m}
              type="button"
              className={viewMode === m ? 'is-active' : ''}
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
            onClick={() => setBasemapMode('satellite')}
          >
            Satellite
          </button>
          <button
            type="button"
            className={basemapMode === 'map' ? 'is-active' : ''}
            onClick={() => setBasemapMode('map')}
          >
            Map
          </button>
        </div>
        <div className="btn-group">
          <button type="button" aria-label="Zoom in" onClick={() => stepZoom(1)}>
            +
          </button>
          <button type="button" aria-label="Zoom out" onClick={() => stepZoom(-1)}>
            −
          </button>
        </div>
      </div>
    </div>
  );
}

const CARD_W = 230;

/** Places a card beside a screen point, flipping to stay inside the viewport. */
function cardTransform(x: number, y: number, el?: HTMLElement | null): string {
  const h = el?.offsetHeight ?? 190;
  const w = el?.offsetWidth || CARD_W;
  const left = x + 18 + w > window.innerWidth - 8 ? x - 18 - w : x + 18;
  const top = Math.min(window.innerHeight - h - 8, Math.max(8, y - h / 2));
  return `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}


function FireCard({
  cluster,
  label,
  spread,
  pinned = false,
  onClose,
  cardRef,
  style,
  fmt,
}: {
  cluster: FireCluster;
  label?: string;
  spread?: Situation['spread'][number];
  fmt: Fmt;
  pinned?: boolean;
  onClose?: () => void;
  cardRef?: React.Ref<HTMLDivElement>;
  style?: React.CSSProperties;
}) {
  const size = cluster.sizeClass ?? 'small';
  const wind = spread?.wind;
  const age = cluster.minHoursAgo < 1 ? '<1 h ago' : `${Math.round(cluster.minHoursAgo)} h ago`;
  // Fire-weather convention: wind is reported by the direction it blows FROM (16-point + degrees);
  // spread is the direction the fire is pushed TOWARD.
  const toDeg = wind ? Math.round(((wind.bearingDeg % 360) + 360) % 360) : 0;
  return (
    <div ref={cardRef} className={`fire-card ${pinned ? 'is-pinned' : ''}`} style={style}>
      <div className="fire-card-head">
        <span className="fire-card-name">{label || cluster.name || FIRE_SIZE_STYLE[size].label}</span>
        {pinned && onClose && (
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={15} />
          </button>
        )}
      </div>
      <dl className="kv">
        <dt>Size</dt>
        <dd>{FIRE_SIZE_STYLE[size].label.replace(/ fire$/, '')}</dd>
        <dt>Detected</dt>
        <dd>{age}</dd>
        <dt>Burned</dt>
        <dd>{fmt.acres(cluster.areaAcres)}</dd>
        {/* Every fire shows the same rows; missing data is "-". */}
        {SPREAD_STEPS.map((h) => {
          const ac = h === 24 ? (spread?.hourly?.acres[24] ?? spread?.acres) : spread?.hourly?.acres[h];
          return (
            <Fragment key={h}>
              <dt>{h} h</dt>
              <dd>{fmt.acres(ac)}</dd>
            </Fragment>
          );
        })}
        <dt>Wind</dt>
        <dd>{wind ? fmt.wind(wind) : '-'}</dd>
        <dt>Spread toward</dt>
        <dd>{wind ? `${compass16(toDeg)} ${String(toDeg).padStart(3, '0')}°` : '-'}</dd>
        <dt>Fuel risk</dt>
        <dd>{cluster.fuelRiskClass != null ? `${cluster.fuelRiskClass}/5` : '-'}</dd>
      </dl>
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
  return `×${Number.isInteger(v) || v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : String(Number(v.toFixed(2)))}`;
}

/** Colour ramp (or class swatches) of a result layer with its range and units, as the backend defines them. */
function Legend({ layer, hint, fmt }: { layer: BurnabilityLayerMeta; hint: string; fmt: Fmt }) {
  const stops = layer.legend?.stops ?? [];
  if (stops.length < 2) return null;
  const [lo, hi] = [stops[0][0], stops[stops.length - 1][0]];
  const factor = layer.units === 'x';
  // Colour stops stay in backend units; only the end labels are converted.
  const m = fmt.measure(layer.units);
  const units = m.unit && m.unit !== '1' ? ` ${m.unit}` : '';
  const neutral = layer.neutral != null && layer.neutral > lo && layer.neutral < hi ? layer.neutral : null;
  const background = layer.discrete
    ? `linear-gradient(to right, ${stops
        .map(([, c], i) => `${c} ${(i / stops.length) * 100}%, ${c} ${((i + 1) / stops.length) * 100}%`)
        .join(', ')})`
    : `linear-gradient(to right, ${stops.map(([v, c]) => `${c} ${((v - lo) / (hi - lo || 1)) * 100}%`).join(', ')})`;
  return (
    <div className="burn-legend">
      <div className="burn-legend-bar" style={{ background }} />
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
            <span>{fmtValue(m.conv(lo))}</span>
            <span>
              {fmtValue(m.conv(hi))}
              {units}
            </span>
          </>
        )}
      </div>
      {hint && <div className="burn-legend-hint">{hint}</div>}
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
  fmt,
}: {
  fmt: Fmt;
  sample: BurnabilityPointResult;
  layers: BurnabilityLayerMeta[];
  selected: BurnabilityLayerId;
}) {
  const ids = [selected, ...POINT_LAYERS.filter((id) => id !== selected)].filter((id) => sample.values[id] != null);
  if (ids.length === 0) return null;
  return (
    <dl className="kv">
      {ids.map((id) => {
        const meta = layers.find((l) => l.id === id);
        const raw = sample.units?.[id] ?? meta?.units ?? '';
        const { unit: units, conv } = fmt.measure(raw);
        return (
          <Fragment key={id}>
            <dt>{meta?.title ?? id}</dt>
            <dd>
              {units === 'x' ? fmtFactor(sample.values[id] as number) : fmtValue(conv(sample.values[id] as number))}
              {units && units !== '1' && units !== 'x' ? ` ${units}` : ''}
            </dd>
          </Fragment>
        );
      })}
    </dl>
  );
}

/** Area after each hour, then the head fire and the wind the run was made with. */
/** The last hour of the same fire with NISAR's fuel state and from the fuel map alone, and how they differ. */
function GrowthCompare({ nisar, alone, fmt }: { nisar: FireGrowth; alone: FireGrowth; fmt: Fmt }) {
  const last = (g: FireGrowth) => [...g.features].sort((a, b) => b.properties.hours - a.properties.hours)[0]?.properties;
  const a = last(nisar);
  const b = last(alone);
  if (!a || !b || !a.area_ha || !b.area_ha) return null;
  const ratio = a.area_ha <= b.area_ha ? `${(b.area_ha / a.area_ha).toFixed(1)}x smaller` : `${(a.area_ha / b.area_ha).toFixed(1)}x larger`;
  return (
    <dl className="kv">
      <dt>
        <i className="growth-swatch" style={{ background: growthColor(a.hours) }} />
        {a.hours} h, with NISAR
      </dt>
      <dd>{fmt.ha(a.area_ha)}</dd>
      <dt>
        <i className="growth-swatch" style={{ background: 'transparent', height: 0, borderTop: '1.5px dashed var(--text)' }} />
        {b.hours} h, fuel map alone
      </dt>
      <dd>{fmt.ha(b.area_ha)}</dd>
      <dt>With NISAR</dt>
      <dd>{ratio}</dd>
    </dl>
  );
}

function GrowthValues({ growth, fmt }: { growth: FireGrowth; fmt: Fmt }) {
  const p = growth.properties;
  const area = new Map(growth.features.map((f) => [f.properties.hours, f.properties.area_ha]));
  const num = (v: number | null, digits: number) => (v == null ? '-' : v.toFixed(digits));
  return (
    <dl className="kv">
      {GROWTH_HOURS.filter((h) => area.has(h)).map((h) => (
        <Fragment key={h}>
          <dt>
            <i className="growth-swatch" style={{ background: growthColor(h) }} />
            {h} h
          </dt>
          <dd title={p.edge_reached ? 'The fire reached the edge of the simulated window' : undefined}>
            {p.edge_reached && h === GROWTH_HOURS[GROWTH_HOURS.length - 1] ? '≥ ' : ''}
            {fmt.ha(area.get(h))}
          </dd>
        </Fragment>
      ))}
      <dt>Head fire</dt>
      <dd>
        {fmt.rate(p.head_rate_m_min)}, {num(p.head_direction_deg, 0)}°
      </dd>
      <dt>Flame</dt>
      <dd>{fmt.short(p.flame_length_m)}</dd>
      <dt>{p.wind_source === 'map_day' ? 'Wind, map day' : 'Wind'}</dt>
      <dd>
        {fmt.ms(p.wind_10m_m_s)} from {num(p.wind_from_deg, 0)}°
      </dd>
    </dl>
  );
}

/** "20 Oct 2026" from a result id such as `ca-20261020-nisar-v3.3.2-ad`. */
function resultDay(id: string): string {
  const m = /(\d{4})(\d{2})(\d{2})/.exec(id);
  if (!m) return id;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
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
