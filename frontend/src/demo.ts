/**
 * Demo scenarios: the app opens one prepared view by itself. Chosen by `?demo=<name>` in the address, else by
 * VITE_DEMO; without either the app starts as usual. A scenario runs only when the backend serves its result.
 */
export interface DemoScenario {
  name: string;
  /** The result it needs; replaces VITE_BURNABILITY_RESULT. */
  result: string;
  state: string;
  layer: string;
  /** Map view after the state has opened: lon, lat and zoom of the 2D view. */
  center: [number, number];
  zoom: number;
  /** A fire to start: the ignition, and the wind (m/s at 10 m, degrees it blows from). */
  fire?: { lon: number; lat: number; wind: { speed: number; from: number } };
  title: string;
  text: string;
  small: string;
  /** Layers of the same day offered as a one-click switch in the note, each with what its colours mean. */
  views?: { layer: string; label: string; legend: string }[];
}

const SCENARIOS: DemoScenario[] = [
  {
    name: 'nisar',
    result: 'smm-20261020-nisar-v3.3.3',
    state: 'CA',
    layer: 'fuel_state',
    center: [-118.8755, 34.1168], // the fire sits right of the note, clear of it at 1440 px and wider
    zoom: 14,
    fire: { lon: -118.8455, lat: 34.1231, wind: { speed: 12, from: 45 } },
    title: 'Woolsey Fire scar, 2018',
    text:
      'The fuel map and optical imagery show mature fuel here; NISAR’s L-band radar still sees regrowth. ' +
      'Same ignition, same wind, two estimates:',
    small:
      'Where a fire started here would be after 1, 3 and 6 hours under this wind; no suppression. ' +
      'The fuel-state scaling is a model assumption, not yet tested against a fire.',
  },
  {
    name: 'la2025',
    result: 'ca-20250107-smap-v3.3.2',
    state: 'CA',
    layer: 'weather_factor',
    center: [-118.35, 34.25],
    zoom: 9,
    title: '7 January 2025',
    text: 'The morning of the Palisades and Eaton fires. The model was trained on 2016 to 2024.',
    small: 'Computed from that day’s analysed weather, not a forecast.',
    views: [
      {
        layer: 'weather_factor',
        label: 'Weather factor',
        legend:
          'Red: the day’s weather multiplies the odds of a large fire (4x and more). White: an ordinary day (1x). ' +
          'Blue: the weather lowers the risk.',
      },
      {
        layer: 'susceptibility_index',
        label: 'Land susceptibility',
        legend: 'The land alone, with the weather of an ordinary day: terrain, fuels, vegetation, surroundings.',
      },
      {
        layer: 'risk_class',
        label: 'Risk class',
        legend: 'Land and weather together: classes 1 to 5 of the chance that an ignition becomes a large fire.',
      },
    ],
  },
];

const named =
  (typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get('demo')) ||
  (import.meta.env.VITE_DEMO as string | undefined) ||
  '';

export const DEMO: DemoScenario | null = SCENARIOS.find((s) => s.name === named) ?? null;

/**
 * Film mode (`?demo=nisar-film`): no app interface at all, one fire grown twice on a still map, from the fuel map
 * alone and with NISAR's fuel state, for recording. By default the two run side by side at the same time
 * (FilmSplit.tsx), followed by a second act with what is measured: the outline of the land that burned in 2018 over
 * the photograph and over the radar layer. `?demo=nisar-truth` loops that second act alone. `layout=sequence` plays
 * the two estimates one after the other (Film.tsx, where `order=on-off` swaps them). `speed=2` plays faster.
 */
export interface FilmScenario {
  result: string;
  layer: string;
  fire: { lon: number; lat: number; wind: { speed: number; from: number } };
  /** Hours the fire runs and the step between two drawn fronts, in minutes. */
  hours: number;
  stepMinutes: number;
  place: string;
  /**
   * The second act of the side-by-side film, what is measured: the land that burned in the old fire, drawn from the
   * result's fuel-age layer (`GET .../scars`), over the photograph on the left and the radar layer on the right.
   */
  scar?: {
    /** Fuel age of that fire on the map day, in years. */
    minYears: number;
    maxYears: number;
    fires: string;
    /** How well each source separates cells burned in that fire from unburned ones (AUC: 1 perfect, 0.5 chance). */
    detection: { nisar: number; optical: number };
    radar: string;
    /** What the photograph under the left half is, with its date. */
    photo: string;
  };
  /** Only the second act, looping (`?demo=nisar-truth`). */
  truthOnly?: boolean;
}

/** `layout=sequence` in the address shows the two estimates one after the other; the default is side by side. */
export const FILM_LAYOUT: 'side-by-side' | 'sequence' =
  typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('layout') === 'sequence'
    ? 'sequence'
    : 'side-by-side';

export const FILM: FilmScenario | null =
  named === 'nisar-film' || named === 'nisar-truth'
    ? {
        result: 'smm-20261020-nisar-v3.3.3',
        layer: 'fuel_state',
        fire: { lon: -118.8455, lat: 34.1231, wind: { speed: 12, from: 45 } },
        hours: 6,
        stepMinutes: 10,
        place: 'Santa Monica Mountains, Woolsey Fire scar (2018)',
        scar: {
          minYears: 7.5, // the Woolsey and Hill fires of November 2018 are 7.9 years old on the map day, 2026-10-20
          maxYears: 8.5,
          fires: 'Woolsey and Hill fires',
          // machine-learning docs/research/gcov.md, "Woolsey + Hill 2018": 26,442 burned cells against 15,292 unburned;
          // AUC of L-band HV 0.89 [0.87, 0.92], of NDVI 0.65 [0.62, 0.70] (LANDFIRE 2025 fuel load 0.58, Sentinel-1 0.81)
          detection: { nisar: 0.89, optical: 0.65 },
          radar: 'NISAR GCOV L-band HV, 47 scenes to 20 October 2026, provisional data',
          // Esri World Imagery reports 10 October and 11 December 2025 for this area at the zoom of the second act
          // (MapServer identify, SRC_DATE2); one level further out it serves an older 15 m picture with the burn in it
          photo: 'satellite photograph, late 2025',
        },
        truthOnly: named === 'nisar-truth',
      }
    : null;
