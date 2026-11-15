# Product


## Platform

web

## Users

**Primary: utility grid operators and emergency planners** (US, California first) who decide which
transmission line sections to de-energize when a forest fire is near. They work under time pressure,
often in a dim control room on large screens. Their job: see which exact sections are dangerous and
when the fire reaches them, so the rest of the grid can stay powered.

**Secondary: hackathon and NASA judges** (NASA Space Apps 2026, challenge "Dancing with the SARs").
They see the app for about 30 seconds in a screen recording, or click through the live deployment,
and must understand "precise, not blanket shutoffs" at a glance.

## Product Purpose

A live map of forest fires near US transmission lines. It forecasts each fire's 24 h spread from the
local wind, tiers the threatened line sections (**SHUT OFF** now, **PSPS** = prepare), and gives each
utility a ready-to-send report. A model map adds NISAR-derived layers: soil moisture (SME2),
backscatter (GCOV), live fuel moisture, large-fire risk, and a click-to-ignite fire-growth tool
(1 / 3 / 6 h isochrones).

It exists because blanket Public Safety Power Shutoffs harm the people they are meant to protect:
whole towns go dark, including Medical Baseline customers on life-support equipment. Success is an
operator isolating only the sections that run through forest near an active fire, and leaving the
rest energized.

Pitch line: "Cutting the power only where wildfire would spread."

## Positioning

Targeted instead of blanket. The unit of decision is a **line section** with a distance, an ETA and a
length in km, not a feeder or a county. NISAR L-band SAR brings soil and vegetation moisture into the
spread picture at 200 m, which utility risk models lack in near real time.

## Operating Context

- Full-screen map tool. The operator picks a state, opens it, reads fires and threatened sections on
  the map, then opens one operator's report and exports it.
- Flow: Overview (US map, state picker: km of lines, km in forest, operators) → State view (fires by
  size class, 24 h spread ellipses, wind arrows, SHUT OFF / PSPS sections, hover or pinned fire card)
  → left panel (Fires list, model map tab named NISAR or Fuel risk, Settings) → Reports (right panel).
- Exports are the hand-off: Copy (text), CSV, GeoJSON, PDF (print view with annotated satellite
  snapshots). There is no email sending; utility contacts are not in the data.
- Demo context: recorded or shown live at 1920×1080 and 1440×900. A laptop must not clip panels.
- Deployed frontend: https://frontend-production-873d5e10.up.railway.app (from `main`).

## Capabilities and Constraints

- **Render-only frontend.** Classification, clipping, spread and weather happen in the backend. The
  only client-side processing is grouping lines into company reports, building exports and drawing
  snapshot images. No analysis and no invented data in the frontend.
- **Stack:** React 19, TypeScript, Vite 8, `@arcgis/core` 5 (SceneView 3D / MapView 2D), lucide-react.
  No state library. No new dependencies unless essential.
- **Must keep working:** the 2D/3D view switch, the report exports, the print/PDF view.
- **Data:** NASA VIIRS hotspots, NIFC perimeters, HIFLD transmission lines, USFS forest mask, NOAA NWS
  hourly wind, Esri World Imagery, model results from `NASA-ACEs/machine-learning`.
- **Honest scope:** transmission lines only. Not modelled: distribution feeders, sectionalizing
  switches, Medical Baseline addresses, population served per section.
- **Honest models:** spread, tiers and costs are heuristics for decision support, not validated fire
  science. The fallback fuel raster is synthetic and must be labelled as such. Fire growth is
  surface-fire potential for one wind, not a forecast.
- **Terminology:** SHUT OFF (act now), PSPS (prepare), operator (the utility; `NOT AVAILABLE` owners
  show as "Unknown operator"), section (the part of a line to de-energize), safety buffer, fire size
  class (large / medium / small), map day, SME2, GCOV, LFMC.
- **Undecided:** the product name. The page title is "US Power Grid"; the pitch has a title line but
  no product name.

## Brand Commitments

- English UI. lucide icons only; no emoji.
- Inter is the brand font; JetBrains Mono carries numbers and units.
- Dark UI over a satellite basemap; floating panels over a full-screen ArcGIS map.
- Voice: calm, technical, credible; an instrument, not a marketing site. Numbers, not sentences.
  No explanatory text in cards.
- Urgency comes only from the data colours: fire red / orange / yellow, shutoff red, PSPS amber,
  wind blue.
- Not this: the generic "AI dashboard" look (purple gradients, nested cards, icon tiles above
  headings, glowing borders, emoji, explanatory paragraphs in cards).

## Evidence on Hand

- Live backend with real fires, wind and line data:
  https://backend-production-27695356.up.railway.app
- Model result bundles (large-fire risk, LFMC, spread, NISAR SME2 and GCOV layers) served by the
  backend branch `feat/real-results`; the frontend asks for `latest` and falls back to the synthetic
  demo `forest-demo-v1` (area `[-120, 37, -119.5, 37.5]`).
- Pitch facts kept with their sources in the presentation, not in the UI: October 2019 shutoffs
  (PSE Healthy Energy 2021), +29 % same-day ER visits for lung patients (PLOS Medicine 2026),
  Camp Fire 2018, Pollock Pines 2019.
- Absent, do not fabricate: utility contacts, customer or population counts per section, Medical
  Baseline locations, validation metrics shown in the UI.

## Product Principles

1. **The section is the answer.** Every screen should lead to which sections, how many km, how far
   the fire is and when it arrives.
2. **Numbers, not sentences.** A value with its unit beats a description of it.
3. **Colour is data.** Chrome stays neutral so red, amber, orange, yellow and blue keep their meaning.
4. **Say what the model is.** Heuristic, synthetic and "not a forecast" are stated where they apply,
   briefly.
5. **Render what the backend says.** If a number is not in the payload, it is not on the screen.

## Accessibility & Inclusion

No formal standard was set. Known needs: legibility over satellite imagery in a dim room and on
large screens, and keyboard-reachable controls. Open decision: a target level (for example WCAG AA).
