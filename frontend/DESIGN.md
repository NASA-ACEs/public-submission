---
name: NASA-ACEs frontend
description: Dark glass panels over a full-screen map. Neutral zinc chrome; colour is reserved for fire, shutoff, PSPS and wind.
colors:
  bg: "#09090b"
  surface: "#111113"
  glass: "rgba(13, 13, 15, 0.86)"
  glass-border: "rgba(255, 255, 255, 0.09)"
  glass-border-strong: "rgba(255, 255, 255, 0.2)"
  hover: "rgba(255, 255, 255, 0.06)"
  active: "rgba(255, 255, 255, 0.13)"
  border: "#27272a"
  border-strong: "#3f3f46"
  text: "#fafafa"
  text-2: "#a1a1aa"
  text-3: "#8e8e98"
  focus: "rgba(250, 250, 250, 0.85)"
  primary-hover: "#e4e4e7"
  shutoff: "#ef4444"
  psps: "#f59e0b"
  fire-large: "#dc2626"
  fire-medium: "#f97316"
  fire-small: "#facc15"
  fire-forecast: "#fca5a5"
  wind: "#93c5fd"
typography:
  headline:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  title:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "13.5px"
    fontWeight: 600
  body:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "-0.006em"
    fontFeature: "'cv11', 'ss01', 'tnum'"
  label:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 500
  label-strong:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 600
  caption:
    fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "11px"
    fontWeight: 400
  data:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
  data-figure:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace"
    fontSize: "15px"
    fontWeight: 500
    letterSpacing: "-0.02em"
  data-display:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace"
    fontSize: "17px"
    fontWeight: 500
    letterSpacing: "-0.02em"
  data-hero:
    fontFamily: "'JetBrains Mono', ui-monospace, 'SFMono-Regular', Consolas, monospace"
    fontSize: "28px"
    fontWeight: 500
    letterSpacing: "-0.02em"
rounded:
  tick: "4px"
  control: "6px"
  card: "12px"
  panel: "16px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "6px"
  md: "8px"
  gutter: "12px"
  panel-x: "16px"
  gutter-bottom: "24px"
components:
  button:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 12px"
  button-hover:
    backgroundColor: "{colors.surface}"
  button-primary:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  segmented:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-2}"
    rounded: "{rounded.control}"
    height: "28px"
    padding: "0 10px"
  segmented-active:
    backgroundColor: "{colors.active}"
    textColor: "{colors.text}"
  input:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 10px"
  check-row:
    textColor: "{colors.text}"
  tab:
    textColor: "{colors.text-2}"
    padding: "8px 0"
  tab-active:
    textColor: "{colors.text}"
  list-item:
    textColor: "{colors.text}"
    padding: "7px 16px"
  list-item-hover:
    backgroundColor: "{colors.hover}"
  list-item-active:
    backgroundColor: "{colors.active}"
  list-item-two-line:
    textColor: "{colors.text}"
    padding: "8px 16px"
  layer-row:
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "5px 8px"
  icon-button:
    textColor: "{colors.text-2}"
    rounded: "{rounded.control}"
    size: "24px"
  icon-button-hover:
    backgroundColor: "{colors.hover}"
    textColor: "{colors.text}"
  panel:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    width: "280px"
  reports-panel:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    width: "328px"
  pill:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    height: "40px"
    padding: "0 16px 0 14px"
  fire-card:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.text}"
    rounded: "{rounded.card}"
    padding: "10px 14px 11px"
  legend:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.text-2}"
    rounded: "{rounded.card}"
    padding: "5px 12px"
  tag:
    backgroundColor: "{colors.active}"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    padding: "1px 7px"
  swatch-shutoff:
    backgroundColor: "{colors.shutoff}"
    rounded: "{rounded.tick}"
    width: "14px"
    height: "4px"
  swatch-psps:
    backgroundColor: "{colors.psps}"
    rounded: "{rounded.tick}"
    width: "14px"
    height: "4px"
  report-line:
    textColor: "{colors.text}"
    padding: "10px 16px 12px"
  report-line-hover:
    backgroundColor: "{colors.hover}"
  report-line-active:
    backgroundColor: "{colors.active}"
  tier-header:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    padding: "10px 16px 8px"
  notice:
    textColor: "{colors.text}"
  tooltip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "4px 8px"
  kv-term:
    textColor: "{colors.text-2}"
  kv-value:
    textColor: "{colors.text}"
---

# Design System: NASA-ACEs frontend

## Overview

**Creative North Star: "The Control-Room Instrument"**

The interface is a set of dark glass panels floating over a full-screen map. The map is the content. The panels are the instrument that reads it. Chrome is neutral zinc at every level: near-black glass, hairline borders, white text in three steps. Nothing in the chrome is coloured.

Colour belongs to the data. Red, orange and yellow mean fire size. Red and amber on a line mean SHUT OFF and PSPS. Light blue means wind. The same hues are used for the map symbols and for their counterparts in the panels, so a dot in a list and a dot on the map read as the same thing. A tier is never coloured text: it is a line swatch beside neutral words.

Density is high and type is small. Labels are set in Inter, readouts in JetBrains Mono, and every figure is tabular. A figure is mono and its unit is a small grey word. Every floating surface is the same glass: one fill, one 1px translucent border, one backdrop blur, with a soft ambient shadow underneath. The system rejects the generic dashboard-template look: purple gradients, nested cards, icon tiles above headings, glowing borders, emoji and explanatory paragraphs in cards.

**Key Characteristics:**
- A full-screen map with floating panels 12px from the top and sides and 24px from the bottom.
- Neutral zinc chrome. Colour appears only for fire, shutoff, PSPS and wind.
- One glass for every floating surface: near-black fill at 86 %, 1px white-alpha border, one backdrop blur.
- Inter for labels and names, JetBrains Mono for readouts, tabular figures everywhere.
- One scale each: five type sizes (11 to 17px) and five radii (4px to 999px).
- A figure is mono with a small grey unit. A tier is a line swatch with neutral text.
- Short motion: 0.1 to 0.15s for state changes, 0.35 to 0.45s for panel entrances.

## Colors

A zinc neutral scale carries all chrome. Seven saturated values are reserved for data. `color-scheme: dark` is set on the root.

### Primary
- **Shutoff Red** (#ef4444): the SHUT OFF tier. Line sections on the map and the line swatch that names the tier in pills, rows, the fire card and the legend. Also the fill of spread ellipses and perimeters at low alpha.
- **PSPS Amber** (#f59e0b): the PSPS (prepare) tier. Line sections on the map and the line swatch that names the tier.

### Secondary
- **Large Fire Red** (#dc2626): large fires. Map dots and rings, the fire dot in lists, the fire card and the legend, the 1 h growth isochrone.
- **Medium Fire Orange** (#f97316): medium fires. Map dots and rings, the fire dot, the 3 h growth isochrone.
- **Small Fire Yellow** (#facc15): small fires. Map dots, the fire dot, the 6 h growth isochrone.
- **Forecast Rose** (#fca5a5): the 24 h forecast. The forecast acres in the fire card and the dashed swatch in the legend.

### Tertiary
- **Wind Blue** (#93c5fd): wind. The rotated arrow and the mph figure in fire rows, report rows and the fire card, and the arrow in the legend.

### Neutral
- **Control-Room Black** (#09090b): page background behind the map, fill of inputs and default buttons, text on the white button.
- **Panel Graphite** (#111113): solid surface for the tooltip, the segmented groups, select options, the hover fill of a default button and the sticky tier headers.
- **Smoked Glass** (rgba(13, 13, 15, 0.86)): fill of every floating surface: both panels, both pills, the legend and the fire card.
- **Glass Edge** (rgba(255, 255, 255, 0.09)): 1px border of floating surfaces and every divider inside them.
- **Glass Edge Strong** (rgba(255, 255, 255, 0.2)): border of a hovered pill and of the pinned fire card, and the rim of a colour chip.
- **Hover Veil** (rgba(255, 255, 255, 0.06)): hover fill of rows and icon buttons, and the placeholder of a report thumbnail.
- **Active Veil** (rgba(255, 255, 255, 0.13)): fill of the selected row and the selected segment, and of a tag.
- **Hairline** (#27272a): border of controls: inputs, buttons, segmented groups and their dividers, the tooltip.
- **Hairline Strong** (#3f3f46): border of a hovered default button and the scrollbar thumb.
- **Text White** (#fafafa): primary text, figures, the fill of the primary button, the active tab underline, the checkbox accent.
- **Text Secondary** (#a1a1aa): labels, sub-lines, inactive tabs, links, icon buttons at rest, placeholders, definition terms, legend text.
- **Text Tertiary** (#8e8e98): unit words, column captions, the tab count, the credit line, coordinates, small inline arrows.
- **Focus White** (rgba(250, 250, 250, 0.85)): the focus ring.
- **Hover White** (#e4e4e7): hover fill of the white button.

### Map symbols
These values live as RGB arrays and hex strings in `src/grid/layers.ts` and `src/grid/burnability.ts`. Most have a satellite and a street-map variant.
- **Draw order, bottom to top:** state outlines, spread ellipses, perimeters, base lines, forest lines, wind arrows, the focused fire, fire dots and rings, line sections, the operator highlight, acre labels. The sections to cut are the answer, so they draw above the fire marks.
- **Line sections:** SHUT OFF 3.4px, PSPS 2.6px, each over a halo 2.5px wider (black 75 % on satellite, white 90 % on the street map).
- **Detections:** 8, 6 and 4px dots in the fire scale at 92 % alpha with a 0.5px dark outline.
- **Fire rings:** large and medium fires only. Fire scale at 14 % fill and a 75 % outline, 1.8px for large and 1.4px for medium. Diameter grows with log10 of acres and is capped at 48px; the medium ring is 0.7 of that.
- **Acre labels:** large fires only, 10px bold with a 1.5px halo, shown at scales larger than 1:1,200,000.
- **Spread ellipse (24 h):** Shutoff Red fill at 7 % alpha, 1.1px short-dash outline in #fecaca at 80 % on satellite and #b91c1c at 75 % on the street map. The focused fire gets a solid 2px outline in #fee2e2, a 16 % fill, and a 56px halo in its own colour with a white rim.
- **Perimeters:** Shutoff Red fill at 12 %, 1.4px outline at 90 %.
- **Wind arrows:** #bfdbfe on satellite, #1e40af on the street map, 1.4 to 3.2px wide by wind speed, over a halo 2.5px wider (black 70 % or white 95 %). Shown at scales larger than 1:2,500,000, so their heads do not outweigh the fires at state scale.
- **Base lines:** 1px, #f4f4f5 at 38 % on satellite, #3f3f46 at 55 % on the street map.
- **Operator highlight:** other layers dim to 30 %. The operator's sections are 4.2px over a 7px black casing, each fire gets a 34px halo with a white rim, and a dashed white line joins the fire to the nearest section.
- **Fire growth:** the fire scale by hour (1 h red, 3 h orange, 6 h yellow), 16 % fill, 1.6px outline at 95 %. The ignition point is a white cross.
- **Model vectors:** #0ea5e9 at 72 %, 1.5px, with a #38bdf8 arrowhead.
- **Forest lines:** user-selected colour (default #15803d), 2.6px over a 4.2px black casing at 55 %.
- **Rasters:** model overlay at 72 % opacity. Tree cover at 30 % on satellite and 22 % on the street map, off by default.
- **Ground:** #0b0f14 until imagery arrives, behind a Control-Room Black scene background.

### Named Rules
**The Colour Is Data Rule.** No chrome element is coloured. Red, amber, orange, yellow and blue appear only where they encode a tier, a fire size, a forecast or wind. Errors and warnings are neutral text with an icon.

**The Same Hue Rule.** A data colour in a panel matches the map symbol it stands for. `--fire-large` equals the large-fire marker RGB (220, 38, 38), and `--shutoff` equals the SHUT OFF section stroke.

**The Swatch Names The Tier Rule.** SHUT OFF and PSPS are named by a line swatch beside neutral text, in every pill, row, card and legend. A tier is never coloured text, a coloured count or a coloured pill.

**The White Is The Action Rule.** The filled button on a surface is white: Text White on Control-Room Black. There is no accent hue for actions.

## Typography

**Body Font:** Inter (variable, optical size 14 to 32, weights 400 to 700; fallback ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto)
**Label/Mono Font:** JetBrains Mono (weights 400 and 500; fallback ui-monospace, SFMono-Regular, Consolas)

Both families load from Google Fonts in `index.html`. There is no display face. Sizes come from one scale on the root: `--fs-micro` 11px, `--fs-small` 12px, `--fs-body` 13.5px, `--fs-figure` 15px, `--fs-title` 17px.

**Character:** One neutral sans for names and labels, one mono for readouts. The pairing is the hierarchy: a label is Inter in grey, its value is mono in white.

### Hierarchy
- **Headline** (600, 17px, line-height 1.25, -0.02em): the state name in the left panel, and "Shutoff reports" or the operator name in the reports panel.
- **Title** (600, 13.5px): line names in report rows. The name in a pill uses the same weight with -0.01em.
- **Body** (400, 13.5px, line-height 1.45, -0.006em): list rows, checkbox labels, inputs. Buttons and the active tab use 500.
- **Label** (500, 12px, Text Secondary): block labels in the left panel. Sub-lines, links, the legend and the summary line use the same size at 400.
- **Label strong** (600, 12px): the sticky tier headers, the head of the fire card, the active segment of a segmented group.
- **Caption** (400, 11px): unit words, hints, column captions, the credit line. Tags use 11px at 500.
- **Data** (JetBrains Mono 400, 12px): the right-hand value of a list row, tier lines, wind readouts, report stats, values in definition lists. Coordinates, ramp ends and the tab count use 11px.
- **Data figure** (JetBrains Mono 500, 15px): figures in pills and the km to cut in a report row.
- **Data display** (JetBrains Mono 500, 17px, -0.02em): key figures in the fire card and in the stat rows at the top of a panel.

### Named Rules
**The Tabular Figures Rule.** `font-feature-settings: 'cv11', 'ss01', 'tnum'` is set on the root. Every figure is tabular, in Inter and in JetBrains Mono alike.

**The Mono For Readouts Rule.** Every value is JetBrains Mono: the right-hand value of a list row, values in definition lists, pill figures, fire card figures, report stats and coordinates. Names, labels and the words of a sub-line stay in Inter.

**The Figure And Unit Rule.** A figure is mono in Text White. Its unit is a separate word in Inter at 11px in Text Tertiary. A label, when there is one, sits under the figure at 12px in Text Secondary.

**The Small And Flat Rule.** Text uses five sizes: 11, 12, 13.5, 15 and 17px. Nothing is larger than 17px. Hierarchy comes from three weights (400, 500, 600) and three text greys.

## Layout

The map fills the viewport (`position: fixed; inset: 0`). Everything else is a fixed, floating element placed against a viewport edge. Two gutters place them: `--gutter` (12px) at the top and sides, `--gutter-bottom` (24px) at the bottom, so floating elements clear the map attribution line.

- **Left panel:** 280px wide, 12px from the top and left, 24px from the bottom (304px with its side gaps, `--sidebar-w`). It is a flex column: a head or search bar, tabs in the state view, a scrolling body and a credit line at the foot. The state summary before Open is compact: as tall as its content. After a state is opened the panel slides out to the left and a pill takes its place.
- **Reports panel:** 328px wide, 12px from the top and right (352px with its side gaps). It is as tall as its content, up to 64px above the bottom edge so the toolbar stays clear. Closed, it is a pill at the top right.
- **Pills:** 40px tall, at the top left (state name and fire count) and top right (reports).
- **Toolbar:** bottom right, 12px from the side and 24px from the bottom. Three segmented groups 28px tall with 6px between them: 3D / 2D, Satellite / Map, zoom in and out.
- **Legend:** 24px from the bottom, starting right of the left panel (`left: var(--sidebar-w)`) or at 12px when the panel is collapsed. It is a column of rows, and its max width leaves room for the toolbar.
- **Fire card:** placed with a transform beside the fire, 18px away, flipped to stay 8px inside the viewport. A pinned card follows the fire as the map moves.
- **Stacking:** panels, legend, toolbar and the reports pill at z-index 30, the panel pill at 31, the fire card at 35, the tooltip at 40.

Inside a panel the horizontal padding is 16px. Rows are 7px 16px (one line) or 8px 16px (two lines). Blocks in the model and Settings tabs stack with a 16px gap and a Glass Edge divider between them, and use 8px between a label and its control. Checkbox lists use 6px gaps. The panel head is 12px 16px 10px and the credit line 8px 16px 10px. A stat row uses 8px 20px gaps between figures.

Responsive behaviour: below 640px the left panel becomes a bottom sheet (full width minus gutters, at most 55vh) and the legend is hidden. Below 900px the reports panel spans the full width between the gutters.

### Named Rules
**The Gutter Rule.** Every floating element sits 12px from the top and side edges and 24px from the bottom edge. The legend starts 12px from the left panel.

**The Content Height Rule.** A panel that holds a few rows is as tall as its content: the state summary and the reports panel. Only a list that scrolls takes the full height.

**The Map Padding Rule.** The map view padding mirrors the panels: 304px on the left (16px when the panel is collapsed), 352px on the right when reports are open, 68px at the bottom, 16px elsewhere. Fly-to targets land in the visible part of the map.

## Elevation & Depth

The system is a hybrid of glass and ambient shadow. Every floating surface uses one recipe: the Smoked Glass fill, a 1px Glass Edge border and `backdrop-filter: blur(18px) saturate(1.2)` (`--blur`). There are two drop shadows, both wide, soft and dark with a negative spread: one for panels and the fire card, which adds an inset 1px highlight along the top edge, and one for pills and the legend. The toolbar groups, the tooltip and the sticky tier headers are solid Panel Graphite with a border and no blur or shadow.

### Shadow Vocabulary
- **Panel** (`box-shadow: 0 1px 0 rgba(255, 255, 255, 0.05) inset, 0 12px 32px -8px rgba(0, 0, 0, 0.55)`): the left panel, the reports panel and the fire card (`--shadow-panel`).
- **Float** (`box-shadow: 0 10px 28px -10px rgba(0, 0, 0, 0.6)`): both pills and the legend (`--shadow-float`).
- **Dot ring** (`box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45)`): a 1px dark ring around fire dots and growth swatches. The large dot adds a 3px ring of its own colour at 30 %.
- **Mark ring** (`box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.55)`): a 1px dark ring around a line swatch, so it reads on glass and over imagery. The tick on the model ramp uses the same ring at 70 %.

### Named Rules
**The One Glass Rule.** Panels, pills, the legend and the fire card share one fill, one border and one blur. No surface has its own alpha or blur radius.

**The Glass Floats, Controls Sit Rule.** Blur and drop shadow belong to floating containers: panels, pills, the legend and the fire card. Controls inside them are flat: a fill, a 1px border, no shadow.

**The Border Does The Work Rule.** A floating surface is separated from the map by its Glass Edge border and the inset top highlight. The drop shadows are ambient and carry no structure.

## Shapes

Corners come from one scale and step down with size: 16px for panels (`--r-panel`), 12px for the fire card and the legend (`--r-card`), 6px for controls, layer rows and report thumbnails (`--r-control`), 4px for small marks such as line swatches, ramps, growth swatches and the scrollbar thumb (`--r-tick`). Pills and tags are fully rounded (`--r-pill`, 999px). Fire dots and colour chips are circles.

Borders are 1px. Tabs carry a 2px underline when active. A selected row is a fill in Active Veil; there is no side bar. A line swatch is a 14px by 4px bar; the forecast swatch is a 2px dashed line. The tick on the model ramp is a 2px white bar that overhangs the 8px ramp by 3px on each side.

### Named Rules
**The Nested Radius Rule.** Radius steps down with size: a 16px panel, a 12px floating card, 6px controls and thumbnails, 4px marks. No other radius exists besides the pill and the circle.

**The Flat Row Rule.** Inside a panel, content is rows separated by a Glass Edge hairline. A row is never a bordered card, and selection is a fill, not a bar.

## Components

The components are quiet and exact: small, flat, neutral, and sized to the text they hold.

### Readouts
- **Figure:** JetBrains Mono 500 in Text White at 17px, or 15px in rows and pills, followed by its unit as an 11px Text Tertiary word with a 4px gap.
- **Stat:** a figure and unit over a 12px Text Secondary label. Stats sit in a wrapping row with 8px 20px gaps. Used for lines, km in forest and operators in the state summary, for NISAR scene counts, and for km to cut per tier in reports.
- **Inline value:** mono at the size of its line, for values inside a sentence-like line such as "Wind 12 mph NW".
- **Wind:** a 12 to 14px arrow rotated to the bearing, then the speed in mono, both in Wind Blue.

### Tiers
- **Swatch:** a 14px by 4px bar with a 4px radius and a 1px dark ring, in Shutoff Red or PSPS Amber.
- **Tier line:** swatch, a count in Text White at weight 500, then km in Text Secondary, all mono 12px. Several tiers wrap with a 14px gap. Used in operator rows and at the foot of the fire card.
- **Tier stat:** a km figure over a label made of the swatch and neutral words ("Shut off · 3 lines").
- **Tier header:** sticky in a report, solid Panel Graphite with a Glass Edge line under it: swatch, the tier name at 600 12px, then count and km in mono Text Secondary on the right.

### Buttons
- **Shape:** gently rounded (6px), 32px tall, 12px horizontal padding, 1px Hairline border, weight 500.
- **Default:** Control-Room Black fill, Text White label. Hover changes the fill to Panel Graphite and the border to Hairline Strong over 0.15s.
- **Primary:** Text White fill and border, Control-Room Black label. Hover dims the fill to Hover White. Used for Open and for PDF.
- **Disabled:** 60 % opacity with a progress cursor; used while a request runs.
- **Export buttons:** the same button in a four-column grid with 6px gaps, set at 12px. Copy, CSV and GeoJSON are default; PDF is primary and shows its progress as a mono count.
- **Segmented group:** an inline row of borderless buttons inside one 6px container with a Hairline border and a Panel Graphite fill. Buttons are 28px tall, at least 44px wide, 12px, Text Secondary, separated by 1px dividers. The active segment has an Active Veil fill and a Text White label at 600. The fill variant stretches to the panel width with equal segments. Used for the time window, the safety buffer, the layer group and the map toolbar.
- **Text link:** no border or fill, 12px, Text Secondary, Text White on hover. Carries a 13 to 14px lucide icon when it navigates.
- **Icon button:** 24px square, 6px radius, transparent, Text Secondary. Hover adds Hover Veil and Text White.
- **Focus:** a 2px Focus White outline at a 2px offset on every focusable element. Inside a segmented group the outline is inset by 2px.

### Inputs / Fields
- **Style:** 32px tall, 6px radius, 1px Hairline border, Control-Room Black fill, 10px horizontal padding. Placeholder in Text Secondary. Selects share the style; their options use Panel Graphite.
- **Focus:** the outline is removed and the border changes to Text Secondary over 0.15s.
- **Unit suffix:** number inputs in the fire-growth controls are mono 12px and carry an 11px unit word inside the field on the right, with 52px of right padding.
- **Invalid:** the border takes Text White.

### Checkbox row
- **Style:** a 14px native checkbox with `accent-color` Text White and an 8px gap to a single-line label that truncates with an ellipsis. Rows stack with 6px gaps.
- **Dense list:** long lists of names (operators) step down to 12px in Text Secondary.

### Navigation
- **Tabs:** text buttons in a row with a 16px gap over a 1px Glass Edge divider. Inactive tabs are Text Secondary, hover is Text White, the active tab is Text White at 500 with a 2px Text White underline that overlaps the divider. The left panel has three: Fires (with a mono count), NISAR or Fuel risk, and Settings.
- **Back link:** the text link with a left arrow, at the top left of a panel.

### List item
- **One line:** a full-width button, 7px 16px, name on the left (truncating), a mono 12px value in Text White with its unit on the right. Hover fills with Hover Veil, the active row with Active Veil. A list may open with an 11px Text Tertiary caption row.
- **Two lines:** 8px 16px with a 2px gap. The first line is the name with an optional fire dot and a mono side value. The second line is 12px Text Secondary with facts separated by middle dots and a wind readout on the right, or a row of tier lines.
- **Layer row:** the one-line item inside a padded list, 5px 8px with a 6px radius. An experimental layer carries the word as a unit.
- **Fire dot:** a circle of 10, 8 or 6px in the fire scale with a 1px dark ring. It stands for fire size wherever a fire is named.
- **Unknown operator:** the name is italic in Text Secondary.

### Cards / Containers
- **Glass panel:** the left panel and the reports panel are one recipe: 16px radius, Smoked Glass fill, Glass Edge border, the one blur, the Panel shadow. Internal dividers are 1px Glass Edge. Content padding is 16px horizontally.
- **Credit line:** the foot of the left panel names the sources in 11px Text Tertiary over a Glass Edge line.
- **Definition list (kv):** a two-column grid, `minmax(0, 1fr) auto`, 5px row gap and 12px column gap, at 12px. Terms are Text Secondary and truncate; values are right-aligned mono in Text White. The row of the selected layer is weight 500. A facts variant puts short terms on the left and lets a long value truncate.
- **Tag:** a fully rounded chip, 1px 7px, Active Veil fill, Text White at 11px and 500. Used for "Synthetic demo" and for the model source in the legend.
- **Notice:** a problem and its way out on one line: a 13 to 14px lucide alert icon in Text Secondary, 12px Text White words, and a text link such as Retry.
- **Tooltip:** solid Panel Graphite, Hairline border, 6px radius, 4px 8px, 12px. It follows the pointer.

### Pills
- **Shell:** 40px tall, fully rounded, Smoked Glass, Glass Edge border, the one blur, the Float shadow. Content is baseline-aligned with an 8px gap. Hover lifts it 1px and brightens the border to Glass Edge Strong; press scales it to 0.97.
- **Panel pill:** a 15px menu icon in Text Secondary (a spinner while loading, an alert when offline), the state name at weight 600, then a figure and its words: the fire count in Data figure followed by "forest fires" at 12px Text Secondary.
- **Reports pill:** "Reports" at weight 600, then the swatch of the leading tier, the line count, the tier name, the km figure and "km". It leads with SHUT OFF and falls back to PSPS; with neither it reads "no lines at risk".

### Fire card
- **Shell:** `width: max-content`, at least 170px, 12px radius, the same glass, border, blur and Panel shadow as a panel, 10px 14px 11px padding. It fades in over 0.16s. Pinned, it accepts pointer events, gains a close icon button, and its border brightens to Glass Edge Strong.
- **Head:** the fire dot and the size as words ("Large fire") at 600 12px, then the age with a 12px clock icon in mono Text Secondary.
- **Stats:** a baseline-aligned row with 18px between groups, each a Data display figure with its unit: burned acres, a 13px arrow, the forecast acres in Forecast Rose with the unit "ac in 24 h"; a 14px arrow rotated to the wind bearing and the speed in Wind Blue; the fuel class out of 5 when the model map provides it.
- **Threat:** under a Glass Edge line, the tier lines for the sections this fire puts at risk.

### Map legend
- **Style:** a column of rows on Smoked Glass with a 12px radius, 5px 12px padding, at least 30px tall, 12px Text Secondary. Hidden below 640px.
- **Symbols row:** three groups separated by Glass Edge vertical dividers: tiers (Shut off, PSPS), fire sizes (Large fire, Medium, Small), forecast (24 h spread, Wind). Each entry is a mark and a label with a 6px gap, 12px apart.
- **Model row:** when the model layer is on the map it takes a row of its own above the symbols: a tag with the source and map day, the layer title in Text White, and a 96px by 8px ramp between its two ends in mono 11px.

### Model tab
- **Source block:** the label "NISAR L-band" over stats (SME2 scenes, share of cells, GCOV scenes), or a "Synthetic demo" tag, then a facts list (map day, model), a "Show on map" checkbox and a link to the model area.
- **Layer block:** the layer groups (Risk, NISAR, Spread, Fuel) as a full-width segmented group, the layers of the group as layer rows, then the ramp.
- **Ramp:** 8px tall with a 4px radius, range ends in mono 11px Text Secondary below it, an 11px hint line, and a white tick at the value of the clicked cell. A ratio layer marks its neutral value under the ramp.
- **Clicked cell and fire growth:** definition lists with mono values. Growth rows carry an 8px swatch in the hour's colour, and the block ends with the hint "Potential for one wind, not a forecast".

### Report line row
- **Style:** a flat full-width button, 10px 16px 12px, with a Glass Edge line under it and no fill. Hover fills with Hover Veil, the selected row with Active Veil.
- **Content:** the line name as Title (two substations joined by a 12px arrow, an unrecorded end in italics) with the km to cut as a Data figure on the right; a wrapping row of mono 12px stats in Text Secondary with 12px lucide icons and Text White values (fire distance, arrival, wind in Wind Blue, fuel class, kV); a 16:7 satellite thumbnail with a 6px radius and a shimmer while loading; one truncated line of coordinates in mono 11px Text Tertiary.

### Motion
- **State changes:** 0.1s for list rows, 0.12s for icon buttons, colour chips and report rows, 0.15s for tabs, inputs, buttons, segmented groups, links and pills.
- **Entrances:** `cubic-bezier(0.22, 1, 0.36, 1)` (`--ease-out`): 0.45s for the left panel, 0.38s for its collapse and for the reports panel, 0.35s for the panel pill. The fire card fades in over 0.16s.
- **Reduced motion:** transitions and animations are switched off for both panels, both pills, the fire card, colour chips, the shimmer and the spinner.

### Known drift
Recorded as found. Nothing here has been changed.
- Control heights (40, 32, 28 and 24px) and most paddings and gaps (4, 6, 8, 12, 16px) are literals in `App.css`. Only the two gutters and three widths are layout tokens.
- Sizes tied to other elements are magic numbers: `344px` and `356px` in the legend's max width (the toolbar plus gutters), `64px` in the reports panel's max height, `24px` in the collapse transform.
- Panel widths are repeated in `App.tsx` (`SIDEBAR_W = 304`, `REPORTS_W = 352`, `CARD_W = 300`) next to `--sidebar-w` and `--reports-w`. Map padding uses 16px at the top, the right and the collapsed left, where the gutter is 12px.
- rgba literals remain in `App.css`: three dark rings for one job (45 % on fire dots and growth swatches, 55 % on line swatches, 70 % on the ramp tick), the large-dot ring `rgba(220, 38, 38, 0.3)` that repeats `--fire-large`, the shimmer band `rgba(255, 255, 255, 0.06)` that repeats `--hover`, and the selection colour `rgba(250, 250, 250, 0.22)`.
- Map symbol colours are repeated outside the tokens: RGB arrays and hex strings in `layers.ts` (`FIRE`, `FIRE_SIZE_STYLE`, `'#ef4444'`, `'#f59e0b'`), `GROWTH_RGB` in `burnability.ts`, hex strings in `snapshot.ts`, and `'#09090b'` for the scene background in `App.tsx`.
- Wind on the map is not `--wind`: arrows are #bfdbfe on satellite and #1e40af on the street map. Panels and the report snapshot use #93c5fd.
- The ground colour `#0b0f14` in `powerGrid.ts` is a blue-black that matches no token; `--bg` is #09090b.
- Forest line presets (#15803d, #22c55e, #065f46, #a3e635, #0ea5e9) are literals in `App.tsx`: four greens and a sky blue outside the data palette, and the user can pick any colour.
- Model vectors are sky blue (#0ea5e9, #38bdf8), a hue with no token that sits close to Wind Blue.
- The fire scale encodes two things: fire size, and the hour of a growth isochrone.
- The acre label on the map is 10px bold, a size and a weight outside the scales.
- The printed report (`src/report/report.ts`) has its own light style: a system font stack instead of Inter and JetBrains Mono, greys #111, #777, #eee and #f2f2f2, sizes 18, 13, 12, 11 and 10px, values not in mono, and a 720:440 image where the panel uses 16:7. Tiers are plain headings with no swatch, and the footer reads "Red: section to de-energize" although PSPS sections are drawn in amber.
- `.input` removes the focus ring and only changes its border to Text Secondary. The invalid state is a brighter border with no icon or text.
- The selected colour chip is a 2px white outline at a 2px offset, the same mark as the focus ring.
- The sticky tier header is opaque Panel Graphite inside a glass panel, so its tone differs from the rows under it.
- Coordinates in a report row join the two ends with a text arrow (→), while line names use a lucide arrow.
- Branch `feat/demo-scenario`, at the product owner's request (no decorative dots, no arrows or symbol characters in interface text): the Reports pill has no swatch; back links ("All states", "All operators") and "Go to model area" are plain text; line names, report coordinates and the fire card's two areas are joined by the word "to"; facts in a row are separated by commas, not middle dots ("Shut off, 3 lines"); the legend names wind with a line swatch; ratios read "0.65x", not "×0.65"; the fire card's title has no fire dot; the 1 / 3 / 6 h swatches are squares. The rotated wind arrow stays where it gives the bearing, and fire dots stay in the list and the legend as the key to the map. The sections above still describe the earlier look.
- One inline layout style remains: `alignSelf: 'flex-start'` on the Synthetic demo tag in `App.tsx`.
- Rules with no matching markup in the current JSX: `.muted`, `.growth-error`.
- Map pop-ups defined for base lines, perimeters, model vectors and growth isochrones use the ArcGIS dark theme, not the panel system.

## Do's and Don'ts

### Do:
- **Do** keep chrome neutral: `bg`, `surface`, `glass`, the three text greys and the two hairlines.
- **Do** use a data colour only for what it encodes: `shutoff`, `psps`, the fire scale, `fire-forecast`, `wind`.
- **Do** set a figure in JetBrains Mono with its unit as a small grey word, for example `12 km`, `8 mph`, `3 h`.
- **Do** name a tier with its line swatch and neutral text.
- **Do** take every colour, font size and radius from a token on `:root`.
- **Do** float containers on the gutters (12px, and 24px at the bottom) with `glass`, `glass-border` and the one blur.
- **Do** keep one white filled button per surface (Open, PDF). Every other button is outlined or text.
- **Do** use lucide icons at 12 to 16px, inline with text.
- **Do** give map strokes a halo, black on satellite and white on the street map, so they read on both basemaps.
- **Do** draw the sections to cut above the fire marks.
- **Do** update the map view padding when a panel changes width or visibility.
- **Do** keep entrances on `cubic-bezier(0.22, 1, 0.36, 1)` and switch them off under `prefers-reduced-motion`.

### Don't:
- **Don't** add an accent hue to chrome. No purple, no gradients as decoration.
- **Don't** colour a tier name, a count or an error. Colour stays with the swatch, the dot and the map.
- **Don't** nest cards, put a bordered card inside a panel, put icons in tiles above headings, or add glowing borders.
- **Don't** use emoji.
- **Don't** write explanatory paragraphs in cards. Show the number.
- **Don't** set text above 17px or at a size outside the five-step scale. One exception: the single figure a demo note is about (`data-hero`, 28px, `--fs-hero`), asked for by the product owner so that the two estimates read from across a room. The film mode (`?demo=nisar-film`, `src/film.css`) is outside this scale altogether: it is a full-frame recording, not the app's interface, and its type follows the frame width so that a 1920 px video and a projector read alike. It keeps the two faces and the colour tokens; the fire's fill and front are drawn on a canvas in the fire hues, and the one clock of the side-by-side layout sits on a glass plate because it lies across the dividing line.
- **Don't** use a radius outside the scale. 8px and 14px are gone.
- **Don't** give a surface its own glass alpha or blur.
- **Don't** add drop shadows to controls inside a panel.
- **Don't** change the font. Inter is the brand face and JetBrains Mono carries the numbers.
