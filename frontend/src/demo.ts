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
  /** A tilted 3D view instead of the flat one: where the camera looks from (degrees from north) and how far it tilts. */
  camera?: { heading: number; tilt: number; center: [number, number]; zoom: number };
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
    name: 'nisar-3d',
    result: 'smm-20261020-nisar-v3.3.3',
    state: 'CA',
    layer: 'fuel_state',
    center: [-118.8755, 34.1168], // the fire sits right of the note, clear of it at 1440 px and wider
    zoom: 14,
    // across the fire's run (it runs to the south-west), from the sea side, so that the ridges do not hide it
    camera: { heading: 330, tilt: 62, center: [-118.853, 34.1185], zoom: 16 },
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
 * (FilmSplit.tsx). Then both halves pull back to the mountain range for a second act: what NISAR sees (the outline of
 * the land that burned in 2018 over the photograph and over the radar layer), then a card of lines over the radar
 * layer. A third act pulls back to the whole state and sets the large fires of the two test years against the two
 * warnings, the model's and the fire-weather index's. `?demo=nisar-truth` loops the scene of the outline alone.
 * `layout=sequence` plays the two estimates one after the other (Film.tsx, where `order=on-off` swaps them).
 * `speed=2` plays faster.
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
   * State whose transmission lines are drawn in the first two acts of the side-by-side film (`GET /v1/grid/states/
   * {abbr}/lines`). Where the growing fire crosses one, that section is marked and the time is told, for each
   * estimate. Lines are named by voltage only: the owners in the public data are not reliable in this area.
   */
  grid?: string;
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
    /** What else of the radar layer is in the frame, for the small caption. */
    note?: string;
    /**
     * Where the halves look in the second act: the longitude of the middle, the latitude of the upper edge and the
     * height on the ground. The radar layer has to fill the frame, so the upper edge and the sides stay inside the
     * result's footprint; below the coast there is only sea, where the layer has nothing to show.
     */
    frame: { lon: number; top: number; heightKm: number };
  };
  /** Only the second act, looping (`?demo=nisar-truth`). */
  truthOnly?: boolean;
  /**
   * The card that closes the second act and the whole third act. Every figure in them is from the test on the
   * ignitions of 2025 and 2026 (see the comments where they are set, and largeFires.ts).
   */
  pitch?: {
    /** Lines of the card over the mountains, coming in one by one, and its small caption. */
    lines: string[];
    caption: string;
    /** The statewide result under the large fires of the third act, and the layer shown dimmed. */
    result: string;
    layer: string;
    /** The two warnings, in the order they are shown. */
    index: string;
    model: string;
    /**
     * Fires that get a name in the last frame: the side of the circle it stands on, other words than the name, and a
     * shift in pixels of a 1920 wide frame where the place next to the circle is taken (a thin line then leads back).
     */
    labelled: Record<string, { side: 'left' | 'right' | 'above' | 'below'; text?: string; dx?: number; dy?: number }>;
    stateCaption: string;
    tagline: string;
    team: string;
  };
}

/**
 * Film mode `?demo=lines-film` (FilmLines.tsx): California's transmission lines against the model's risk map, every
 * 200 m along every line, then a push in to the stretch where the pieces in the top two classes are densest.
 */
export interface LinesFilm {
  result: string;
  layer: string;
  /** Pieces of line with their risk class, made by presentation/lines/prep_lines.py from the backend's sampler. */
  data: string;
  /** Name of the region of the push in (its centre and size are in the data). */
  region: string;
  lines: string[];
  tagline: string;
  team: string;
  caption: string;
}

export const LINES_FILM: LinesFilm | null =
  named === 'lines-film'
    ? {
        result: 'ca-20261020-nisar-v3.3.2-ad', // California, 200 m cells, the map of 2026-10-20
        layer: 'risk_class',
        // 4,950 HIFLD lines, 66,464 km; 40,730 km on land the map rates; 4,481 km in classes 4 and 5 (3,699 + 782)
        data: `${import.meta.env.BASE_URL}film/lines-ca-20261020.json`,
        // centre 118.693 W 34.686 N, 110 km wide: Castaic, Lake Hughes and Gorman, between Santa Clarita and the
        // Antelope Valley; the 110 km window of the state with the most km of line in classes 4 and 5
        region: 'North of Los Angeles',
        lines: ['Every 200 m of every line. Every day.'],
        tagline: 'Cut the power only where a spark would grow.',
        team: 'NASA-ACEs',
        // classes 4 and 5: machine-learning docs/research/out-of-time.md, 64 of the 80 large fires of 2025 and 2026
        caption:
          'Transmission lines: HIFLD public data, routes approximate. Risk: model map for 20 October 2026, 200 m ' +
          'cells, sampled every 200 m along each line. Classes 4 and 5 are where 64 of the 80 large fires of 2025 ' +
          'and 2026 started.',
      }
    : null;

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
        grid: 'CA', // a 66 kV line passes 0.7 km from the ignition
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
          // fuel age of the result there on 2026-10-20 is 1.7 to 1.9 years: the Franklin fire (December 2024) and the
          // Palisades fire (January 2025)
          note: 'Regrowth south-east of the outline: fires of 2024 and 2025.',
          // The result's footprint is a UTM rectangle of 41.9 by 25.1 km, turned by 0.8 degrees on the map: its upper
          // edge runs from 34.2000 N in the west to 34.2064 N in the east, its sides are near 118.955 W and 118.498 W,
          // and its lowest 2.8 km are sea without data. This frame is 26.7 by 30 km at 1920 by 1080: 0.6 km under the
          // upper edge, 4.5 km and 9 km inside the sides, and it runs out over the sea in the south.
          frame: { lon: -118.755, top: 34.195, heightKm: 30 },
        },
        truthOnly: named === 'nisar-truth',
        pitch: {
          lines: [
            // the statewide map has 200 m cells and is made for one day at a time
            'Every 200 m of California. Every day.',
            // machine-learning docs/research/out-of-time.md: risk classes 4 and 5 are 14.5 % of the 13,296 ignitions of
            // 2025 and 2026 and hold 64 of the 80 fires that grew past 400 ha
            '80 % of large fires started where our model warned.',
            // the same number of ignitions (1,931, the 14.5 % ranked highest) taken by the gridMET burning index holds
            // 26 of the 80: 64 / 26 = 2.46, shown as 2.5 (largeFires.ts has the fires and both flags)
            '2.5x more than the standard fire-weather index.',
          ],
          caption: 'Trained on 2016 to 2024. Tested on 13,296 California ignitions of 2025 and 2026.',
          result: 'ca-20261020-nisar-v3.3.2-ad', // California, 200 m cells, the map of 2026-10-20
          layer: 'risk_class',
          index: 'Standard fire-weather index',
          model: 'Our model',
          labelled: {
            // the largest fire of the two years; its ignition was in class 3 and outside the index's 14.5 % as well
            Gifford: { side: 'left', text: 'Gifford, missed by both' },
            Bug: { side: 'right' },
            Biscar: { side: 'right' },
            Garnet: { side: 'left' },
            Palisades: { side: 'below' },
            Eaton: { side: 'right', dx: 96 }, // past the smaller fires east of it
          },
          stateCaption:
            'Large fires of 2025 and 2026 (400 ha or more). Warned: among the 14.5 % of ignitions each method ranked ' +
            'highest. Model trained on 2016 to 2024.',
          tagline: 'Cut the power only where a spark would grow.',
          team: 'NASA-ACEs',
        },
      }
    : null;
