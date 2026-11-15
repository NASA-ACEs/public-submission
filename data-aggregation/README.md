<!-- fuel-lake — project README -->
<!-- Licensed under Apache-2.0. -->

# fuel-lake

fuel-lake is the data layer of a NASA Space Apps 2026 project: **soil moisture → live fuel moisture (LFMC) → fire
spread risk** for California. It downloads satellite, weather, fuel and fire data (NISAR, SMAP, GEDI, Sentinel-1/2,
LANDFIRE, gridMET, HRRR, ERA5-Land, FPA-FOD, CAL FIRE, FIRMS, Globe-LFMC, FEMS). It stages them in analysis-ready
form (COG, Zarr, GeoParquet) and catalogs everything as a static STAC catalog. It then builds gold training
tables and cubes, and lets the team's two systems read the lake and publish their outputs back into it. **The
lake holds data inside California only.** The region is the US Census 2025 state boundary
(`config/regions/california.geojson`), and every output is clipped to it. There are two AOIs: **`ca`**, the whole
state on EASE-Grid 2.0 (EPSG:6933) at 200.179 m, cell-aligned to NISAR SME2 (4956 × 4827 cells), and **`smm`**,
the Santa Monica Mountains at 30 m (EPSG:32611, 1398 × 837 px). The binding contract is
[ARCHITECTURE.md](ARCHITECTURE.md); the consumer guide is [docs/microservices.md](docs/microservices.md). Where
their dataset lists or statuses differ from the configs, **the inventory below (generated from the configs)
supersedes them**.

Contents: [Quick start](#quick-start) · [Data inventory](#data-inventory) · [Getting data out](#getting-data-out)
· [System A guide](#guide-for-system-a-fuel-state) · [System B guide](#guide-for-system-b-fire-spread-risk) ·
[Running on a server](#running-on-a-server) · [Validation status](#validation-status) ·
[Known gaps](#known-gaps-and-limitations) · [Licence](#licence)

## Quick start

```bash
git clone <repo-url> fuel-lake && cd fuel-lake
uv sync                                   # Python 3.12 + all dependencies from uv.lock (GDAL/HDF5 in the wheels)
export LAKE_ROOT=/data/fuel-lake          # default: ../fuel-lake-data next to the repo
uv run fuellake init                      # lake directories, STAC root catalog, one collection per dataset
uv run fuellake datasets                  # registered datasets, status, licence
uv run fuellake run fire-ignitions --aoi ca --start 2016-01-01 --end 2024-12-31   # one dataset, keyless
scripts/full_run.sh                       # everything, in order (see "Running on a server")
```

## Data inventory

Generated from `config/datasets/*.yaml` on 2026-11-14; only the *Period* column is written by hand. `form A` =
one single-band COG per variable on the AOI grid (`staged/<dataset>/<aoi>/<item>/<var>.tif`). `form B` = a monthly
Zarr store on the source's native grid (`.../data.zarr`, dims `time, y, x`). Tables are GeoParquet
(`staged/tables/<table>/<dataset>/part-*.parquet`). `local` datasets run only on `smm` (30 m statewide would be
about 471 M cells per layer); `region` datasets also run on `ca`. Status values: `tested` (a real run produced
cataloged items, the rerun was a no-op and `validate` passed), `search-only` (discovery tested live, download and
transform tested on synthetic files only), `unverified` (offline tests only), `interface` (written by the team's
systems).

| Dataset | What it is | Variables (unit) | Grid / resolution | Cadence | Period | AOI scales | Credentials | Status | Licence (redistributable) |
|---|---|---|---|---|---|---|---|---|---|
| `biomass` | Flammable biomass (team biomass subsystem drop zone) | `biomass` (kg/m2), `biomass_uncertainty` (kg/m2) | form A COG on the AOI grid; native as delivered | on drop | on drop | local, region | none | tested | unknown (no) |
| `cop-dem` | Copernicus DEM GLO-30 elevation, slope and aspect | `elevation` (m), `slope` (degree), `aspect` (degree) | form A COG on the AOI grid; native 30 m (1 arc-second; about 25.6 m x 30.9 m at 34 N) | static (Copernicus DEM 2021 release) | static (2021 release) | local | none | tested | Copernicus-WorldDEM-30-Licence (yes) |
| `era5-land-daily` | ERA5-Land daily aggregates (2 m temperature, dew point, precipitation, 10 m wind, soil water layers 1-4) | `t2m` (K), `t2m_max` (K), `d2m` (K), `tp` (mm), `u10` (m/s), `v10` (m/s), `ws10` (m/s), `ws10_max` (m/s), `swvl1` (m3/m3), `swvl2` (m3/m3), `swvl3` (m3/m3), `swvl4` (m3/m3) | form B Zarr, native grid 0.1 degree (about 9 km), regular lat/lon | daily, hourly source (CDS catalogue extent ends 2026-11-08 on 2026-11-14, about 6 days latency) | 1950-today at the CDS; full_run.sh: 2025-today (history with ERA5_HISTORY=1) | local, region | `CDSAPI_URL`, `CDSAPI_KEY` | tested | CC-BY-4.0 (yes) |
| `features-cube` (gold) | fuel-lake gold datacube (AOI grid; time, y, x) | see item `fuellake:variables` | form B Zarr, native grid AOI grid (30 m for smm) or a coarser requested resolution | on build (fuellake build-features) | build period | local, region | none | unverified | various (no) |
| `fems-lfmc` | USDA FEMS live fuel moisture field samples | see table columns | table `lfmc_samples` (GeoParquet points/polygons) | irregular (field sampling per site, typically every 2-4 weeks in season) | full_run.sh: 2015-10-today (source holds older years) | local, region | none | tested | unknown (no) |
| `fire-ignitions` | California wildfire ignitions with final size (USFS FPA-FOD 7th edition, plus NIFC WFIGS after its last year) | see table columns | table `fire_ignitions` (GeoParquet points/polygons) | FPA FOD editions (7th edition FPA_FOD_20260615, 1992-2024); WFIGS refreshed every 5 minutes | FPA-FOD 1992-2024 + WFIGS 2025-today | local, region | none | tested | other (no) |
| `fire-perimeters` | Historical fire perimeters (CAL FIRE FRAP, plus NIFC WFIGS after the last FRAP year) | see table columns | table `fire_perimeters` (GeoParquet points/polygons) | FRAP annual release (latest year 2025, layer data edited 2026-05-21); WFIGS updated continuously | FRAP all years (to 2025) + WFIGS after | local, region | none | tested | other (no) |
| `firms` | NASA FIRMS active fire detections (MODIS, VIIRS) | see table columns | table `fire_detections` (GeoParquet points/polygons) | archive yearly (latest 2025, files re-published 2026-10-10); NRT via API within hours | MODIS 2000-, VIIRS 2012-; archive to 2025, API for 2026 | local, region | `FIRMS_MAP_KEY` | tested | other (no) |
| `fuel-state` (interface) | Fuel state (System A output) - flammable dry fuel load, LFMC and spread potential on the AOI grid | `fuel_load` (kg/m2), `lfmc` (%), `spread_potential` (1), `fuel_load_p05` (kg/m2), `fuel_load_p95` (kg/m2), `lfmc_p05` (%), `lfmc_p95` (%) | form A COG on the AOI grid; native the AOI grid (smm 30 m UTM, ca 200.179 m EASE-Grid 2.0 = the NISAR SME2 grid) | on publish | on publish | local, region | none | interface | unknown (no) |
| `gedi-l4b` | GEDI L4B gridded aboveground biomass density, Version 2.1 (1 km, 2019-04-18 to 2023-03-16) | `agbd` (Mg/ha), `agbd_se` (Mg/ha), `quality_flag` (-), `n_samples` (1) | form A COG on the AOI grid; native 1000.895 m (EASE-Grid 2.0 global, EPSG:6933) | static (one-time estimate from mission weeks 19-223, 2019-04-18 to 2023-03-16) | 2019-04-18..2023-03-16 (one estimate) | local, region | `EARTHDATA_TOKEN` | tested | CC0-1.0 (yes) |
| `globe-lfmc` | Globe-LFMC 2.0 live fuel moisture database | see table columns | table `lfmc_samples` (GeoParquet points/polygons) | static (published 2024-03-25; samples 1977-2023) | 1977-2023 | region | none | tested | CC0-1.0 (yes) |
| `gridmet` | gridMET daily wind, humidity, VPD, precipitation, temperature, reference ET, ERC, BI and dead fuel moisture | `vs` (m/s), `th` (degree), `fm100` (%), `fm1000` (%), `rmin` (%), `vpd` (kPa), `erc` (1), `pr` (mm), `tmmx` (K), `pet` (mm), `bi` (1) | form B Zarr, native grid 1/24 degree (about 4 km), EPSG:4326 cell centres | daily (latest day 2026-11-12 on 2026-11-14, i.e. about 2 days latency) | 1979-today (~2 d latency) | local, region | none | tested | CC0-1.0 (yes) |
| `hrrr-gusts` | HRRR daily maximum surface wind gust and 10 m wind speed (sampled analyses, 3 km) | `gust_max` (m/s), `wind10_max` (m/s) | form B Zarr, native grid 3 km (HRRR CONUS Lambert conformal grid, 1799 x 1059 cells) | daily (gridMET day 07 UTC to 07 UTC; HRRR runs hourly, about 1 h latency) | full_run.sh: HRRR_START (2025-01)-today | local | none | tested | other (yes) |
| `landfire-canopy` | LANDFIRE forest canopy cover, height, base height and bulk density (LF 2025) | `canopy_cover` (%), `canopy_height` (m), `canopy_base_height` (m), `canopy_bulk_density` (kg/m3) | form A COG on the AOI grid; native 30 m (EPSG:5070 CONUS Albers) | static (annual LANDFIRE releases; this config pins LF 2025) | LF 2025 | local | none | tested | CC0-1.0 (yes) |
| `landfire-evt` | LANDFIRE Existing Vegetation Type (EVT), shares of 16 vegetation classes, releases LF 2016, LF 2022 and LF 2025 | `evt_share_<class>` (1) for chaparral, coastal_scrub, sagebrush, desert_scrub, other_shrub, oak_woodland, other_hardwood, conifer_oak, dry_conifer, mesic_conifer, juniper_pinyon, grass, riparian, sparse, agriculture, developed | form A COG on the AOI grid; native 30 m (EPSG:5070 CONUS Albers) | static (LANDFIRE releases LF 2016, LF 2022, LF 2025; each item dated 1 January of its release year) | LF 2016, LF 2022, LF 2025 | local, region | none | tested | US-Public-Domain (yes) |
| `landfire-fbfm40` | LANDFIRE 40 Scott and Burgan fire behaviour fuel models (FBFM40), releases LF 2016, LF 2022 and LF 2025 | `fuel_model` (-) | form A COG on the AOI grid; native 30 m (EPSG:5070 CONUS Albers) | static (LANDFIRE releases LF 2016, LF 2022, LF 2025; each item dated 1 January of its release year) | LF 2016, LF 2022, LF 2025 | local | none | tested | US-Public-Domain (yes) |
| `landfire-fuel-load` | Surface fuel load, fuel bed depth and moisture of extinction from LANDFIRE FBFM40 (Scott and Burgan 2005 Table 7) | `total_load` (kg/m2), `dead_1h_load` (kg/m2), `dead_10h_load` (kg/m2), `dead_100h_load` (kg/m2), `live_herb_load` (kg/m2), `live_woody_load` (kg/m2), `fuel_bed_depth` (m), `moisture_of_extinction` (%), `burnable` (-) | form A COG on the AOI grid; native 30 m (the FBFM40 item's AOI grid; no resampling) | static (one item per landfire-fbfm40 release in the same lake, LF 2016, LF 2022 and LF 2025; same dates as the source items) | per FBFM40 release | local | none | tested | US-Public-Domain (yes) |
| `landfire-topo` | LANDFIRE 2020 bare-earth elevation, slope (degrees) and aspect (from USGS 3DEP) | `elevation` (m), `slope` (degree), `aspect` (degree) | form A COG on the AOI grid; native 30 m (EPSG:5070 CONUS Albers; from the 1 arc-second 3DEP DEM) | static (LANDFIRE LF 2020 topographic products, published 2022-01-31; "rarely updated", still current in 2026) | LF 2020 (static) | local | none | tested | US-Public-Domain (yes) |
| `matchups` (gold) | fuel-lake gold label x feature matchup table (LFMC samples x raster features) | see table columns | table `matchups` (GeoParquet points/polygons) | on build (fuellake build-features) | build period | local, region | none | unverified | various (no) |
| `nisar-gcov` | NISAR L2 GCOV terrain-corrected backscatter (HH, HV) | `gamma0_hh_db` (dB), `gamma0_hv_db` (dB) | form A COG on the AOI grid; native 10 m (40 MHz mode over California) | 12 days | HH+HV over CA since 2026-06 (PROVISIONAL) | local | `EARTHDATA_TOKEN` | search-only | CC0-1.0 (yes) |
| `nisar-sme2` | NISAR L3 soil moisture (SME2) | `soil_moisture` (m3/m3), `soil_moisture_uncertainty` (m3/m3), `soil_moisture_quality_flag` (1), `soil_moisture_dsg` (m3/m3), `soil_moisture_pmi` (m3/m3), `soil_moisture_tsr` (m3/m3), `sm_algorithm_spread` (m3/m3) | form A COG on the AOI grid; native 200 m (EASE-Grid 2.0 Global, 200.179 m cells) | 12 days | 2026-06-today (PROVISIONAL) | local, region | `EARTHDATA_TOKEN` | tested | CC0-1.0 (yes) |
| `opera-rtc-s1` | OPERA RTC-S1 radiometric terrain-corrected Sentinel-1 backscatter (VV, VH) | `gamma0_vv_db` (dB), `gamma0_vh_db` (dB) | form A COG on the AOI grid; native 30 m | 6 days per track (Sentinel-1C and 1D; Sentinel-1A ended 2026-06-30) | full_run.sh: 2025-today | local | `EARTHDATA_TOKEN` | search-only | CC0-1.0 AND Copernicus-Sentinel-legal-notice (yes) |
| `point-features` (gold) | fuel-lake gold point-feature table (fire ignitions and LFMC samples x native-grid features) | see table columns | table `point_features` (GeoParquet points/polygons) | on build (fuellake build-points) | build period (ignitions 2016-2024) | local, region | none | unverified | various (no) |
| `sentinel2-indices` | Sentinel-2 L2A vegetation indices (NDVI, NDMI) | `ndvi` (1), `ndmi` (1) | form A COG on the AOI grid; native 10 m (NDVI, B04/B08) / 20 m (NDMI, B8A/B11) | about 5 days (2-3 satellites; cloud-dependent) | full_run.sh: 2015-10-today | local | none | tested | Copernicus-Sentinel-Data-Legal-Notice (yes) |
| `smap-climatology` | SMAP surface soil moisture day-of-year climatology (31-day window mean, std and count, 9 km, 2015-10..2024-12) | `l4_sm_surface_mean` (m3/m3), `l4_sm_surface_std` (m3/m3), `l4_sm_surface_count` (1), `l3e_sm_am_all_mean` (m3/m3), `l3e_sm_am_all_std` (m3/m3), `l3e_sm_am_all_count` (1), `l3e_sm_am_mean` (m3/m3), `l3e_sm_am_std` (m3/m3), `l3e_sm_am_count` (1) | form B Zarr, native grid 9 km EASE-Grid 2.0 Global (EPSG:6933, 9008.0552 m cells) | static (day-of-year climatology of 2015-10-01..2024-12-31; rebuilt when an input month changes) | baseline 2015-10-01..2024-12-31 | local, region | none | unverified | CC0-1.0 (yes) |
| `smap-l3e` | SMAP enhanced L3 passive surface soil moisture (SPL3SMP_E v006, 9 km, daily AM and PM retrievals) | `l3e_sm_am` (m3/m3), `l3e_sm_am_all` (m3/m3), `l3e_sm_pm` (m3/m3) | form B Zarr, native grid 9 km EASE-Grid 2.0 Global (EPSG:6933, 9008.0552 m cells; posted from 36 km radiometer footprints by Backus-Gilbert interpolation) | daily (one granule per UTC day with the 6 a.m. descending and 6 p.m. ascending half orbits); about one day processing latency (newest day 2026-11-12 on 2026-11-14) | 2015-03-today (~1 d latency) | local, region | `EARTHDATA_TOKEN` | tested | CC0-1.0 (yes) |
| `smap-l4` | SMAP L4 surface and root-zone soil moisture (SPL4SMGP v008, 9 km, daily sample) | `sm_surface` (m3/m3), `sm_rootzone` (m3/m3), `sm_profile` (m3/m3), `smap_sm` (m3/m3) | form B Zarr, native grid 9 km EASE-Grid 2.0 Global (EPSG:6933, 9008.0552 m cells) | 3-hourly source (8 granules per day), staged as one granule per day (13:30 UTC); about 2 days processing latency (newest day 2026-11-10 on 2026-11-14) | 2015-03-today (~2 d latency) | local, region | `EARTHDATA_TOKEN` | tested | CC0-1.0 (yes) |
| `spread-risk` (interface) | Fire-spread risk (System B output) - risk index and class on the AOI grid for a validity window | `risk_index` (1), `risk_class` (-), `rate_of_spread` (m/min), `risk_p05` (1), `risk_p95` (1) | form A COG on the AOI grid; native the AOI grid (smm 30 m UTM, ca 200.179 m EASE-Grid 2.0 = the NISAR SME2 grid) | on publish | on publish | local, region | none | interface | unknown (no) |

### Per-dataset notes (read before modelling)

- **`fire-ignitions`**: 96,818 California ignitions for 2016-01-01..2026-11-14. FPA-FOD 7th edition
  (FPA_FOD_20260615) covers 2016–2024 with 74,568 rows (5 CA-tagged points outside California were dropped). WFIGS
  Incident Locations cover 2025–2026. Rows per year:
  2016 7,892 · 2017 9,571 · 2018 9,519 · 2019 6,546 · 2020 10,360 · 2021 7,187 · 2022 7,120 · 2023 8,191 ·
  2024 8,182 · WFIGS 2025 13,070 · 2026 9,180. Of the 2016–2024 ignitions, 476 reached ≥ 1,000 ac and **477
  reached ≥ 400 ha (988.42 ac), 0.64 %**. Years are split at Pacific midnight. The dedupe rule is 2 km / ±2 days,
  with 0 duplicates at the 2024/2025 boundary. Known-fire checks: Camp (2018-11-08, 153,336 ac), Woolsey
  (96,949 ac), August Complex (41 fires, 1,031,896 ac).
  **Train on 2016–2024 only.** WFIGS 2025–2026 is not comparable with FPA-FOD:
  - `IncidentSize` is null for 36–46 % of rows;
  - a fire-out date is filled for only 9 % of 2025 rows, so sizes may not be final;
  - `FireCauseGeneral` is null for 99.8 % (the cause comes from `FireCause`);
  - WFIGS holds thousands of small LA County records that FPA-FOD lacks (4,589 in 2024).

  The WFIGS location is the layer geometry; `initial_lat`/`initial_lon` are kept (914 null, 1,302 more than 2 km
  away, 12 outside California; option `wfigs_location: initial`).
- **`fire-perimeters`**: CAL FIRE FRAP plus WFIGS after the last FRAP year. Perimeters that cross the state line
  are kept whole. On `ca` the geometries are generalised by about 50 m. `area_acres` is the whole fire.
- **`gridmet`**: 11 variables. `pr`, `vpd`, `pet`, `tmmx`, `fm100` and `fm1000` are flagged `lag: true`. **Lag
  columns are single-day values (`<var>_lag<L>d` = the value on day `date − L`)**: compute 72 h precipitation or
  growing degree days from the daily series. `tmmx` is in K. `th` is circular. The raw `ca` download includes
  neighbouring cells; the staged copy is masked to California. Items staged with the old 7 variables fail
  `validate` until they are rerun with `--force`. The connector serialises requests per variable: never run two
  gridMET jobs at once (THREDDS corrupts concurrent responses).
- **`smap-l4`** is gap-free model output with assimilation (daily 13:30 UTC granule). It is staged on `ca` for
  2015-10-01..2026-11-14 (134 monthly items, 4,059 days, about 463 MB). **`smap_sm` is provisionally an alias of
  L4 `sm_surface`** (config `alias_of`).
- **`smap-l3e`** is a retrieval with swath, outage and quality gaps: 84 days have no granule (2019-06-20..07-22,
  2022-08-07..09-19, plus 7 single days). Recommended-quality `l3e_sm_am` covers 15 % of cell-days and 0 % in 6 of
  7 Southern California chaparral cells, so use `l3e_sm_am_all` or L4 in chaparral. Against L4 (Oct–Nov 2015):
  r = 0.57 with bias +0.061 for all retrievals; r = 0.72 with bias +0.033 for recommended ones.
- **`smap-climatology`**: day-of-year mean, std and count over 2015-10-01..2024-12-31 (31-day circular window;
  blank where the count is below 30). The item is dated 1904 on purpose: find it by collection, without a date
  filter. **It must be built on the server after the full L3E run.**
- **`nisar-sme2`** (PROVISIONAL only) has a **wet bias of +0.110 m³/m³** at 21 NEON sites in Jun–Jul 2026 (Shi et
  al. 2026, doi:10.3390/rs18213777). The same study finds pooled R −0.038 and within-site anomaly R 0.259, so use
  anomalies, not absolute values. The SMAP climatology baseline is the team's choice; the paper uses within-site
  anomalies. `sm_source = DSG`, because only DSG is present on every smm date and the top-level product switches
  algorithm mix in 29 % of cells. The flag bits were verified on real files (bit 0 = not recommended; bits 1–3 =
  algorithms used); no cell is masked. Assets: `soil_moisture`, `soil_moisture_uncertainty`,
  `soil_moisture_quality_flag`, `soil_moisture_dsg`, `soil_moisture_pmi`, `soil_moisture_tsr` and
  `sm_algorithm_spread` (max − min of the three algorithms; a practical uncertainty). On `ca` the data are copied
  cell by cell (bit-identical). California needs 26 descending + 26 ascending granules per 12-day cycle, at about
  13 MB download and about 33 MB on disk each. The `smm` items made before the status change carry
  `fuellake:status: search-only` until rerun with `--force`.
- **`landfire-fbfm40` / `landfire-fuel-load`**: one item per release (LF 2016, LF 2022, LF 2025), staged keylessly.
  **Post-fire releases encode the fire.** For the 2018 Woolsey fire, the share of high-load shrub (SH5/7/8/9, TU5)
  inside the perimeter versus an unburned control was: LF 2016 3.4 % vs 1.9 %; LF 2022 0.0 % vs 36.7 %; LF 2025
  3.0 % vs 37.1 %. The mapping method also changed: 70.7 % of unburned control pixels change their code between
  LF 2016 and LF 2022, so treat the release as a covariate. Fuel loads are the Scott & Burgan 2005
  (RMRS-GTR-153, Table 7, p. 18) nominal values per model, in short tons: 1 t/ac = 0.2241702 kg/m². The
  public-domain status of the publication's table is assumed. LANDFIRE is not staged on `ca` (about 3.1 GB per
  release at 30 m); `build-points` reads it remotely per point.
- **`landfire-topo`** (LF 2020, bare earth from 3DEP) is preferred over **`cop-dem`**, a DSM that reads +0.5 m on
  grass and +3.7 m under forest, has a slope RMSE of 2.7° and is shifted 10–20 m E/SE. In a cube that holds both,
  the variables are prefixed (`landfire_topo__elevation`, `cop_dem__elevation`).
- **`landfire-canopy`** covers forest canopy only: chaparral reads 0 (only 7 % of `smm` is forested).
- **`landfire-evt`** is not the EVT code but the share of 16 vegetation classes, one asset per class
  (`evt_share_chaparral`, …, `evt_share_developed`). Each of the 1,088 EVT codes is assigned to a class by rule
  from the official attribute table (`fuellake.connectors.landfire_evt.classify`; the table is the item's
  `classes` asset). On `ca` (200 m) an asset is the share of the class in the cell, in steps of 1/200; on `smm`
  (30 m) it is 0 or 1, so average it over a window to get a share. It is the one LANDFIRE dataset that runs on
  `ca`: 60 blocks of raw 30 m clips per release (about 210 MB, LZ77), reused on a rerun. Pick the release with
  `fuellake:valid_from` (2017-01-01, 2022-10-01, 2025-10-01) so that a fire does not see its own burn; 2016 has no
  release.
- **`gedi-l4b`** is 1 km, 2019-04-18..2023-03-16, and nearest-upsampled on `smm`. It is aboveground live biomass,
  not surface fuel, and is coarse for chaparral; check `agbd_se` and `quality_flag`.
- **`hrrr-gusts`** (`smm` only) are sampled maxima. The 00Z + 12Z samples recover about 82 % of the daily maximum
  gust and 85 % of the maximum wind (calibrated on 4 days); treat them as lower bounds.
- **`fems-lfmc`**: months stay open for 45 days (`settle_days`) and are refreshed. FEMS includes the legacy NFMD
  data, so it overlaps Globe-LFMC for 2017–2023: do not double-count.
- **`firms`**: use `detection_type == 0` for vegetation fires. The current year needs `FIRMS_MAP_KEY`.
- **`era5-land-daily`** has been run on one month only (`ca` 2026-10). It comes from hourly `reanalysis-era5-land`;
  `tp` is in mm, and the day-D total is the 00 UTC value of D+1. Variables include `t2m_max`, `ws10`, `ws10_max`
  and `swvl1`–`swvl4`. Against gridMET (Oct 2026): `t2m_max` vs `tmmx` r 0.955, bias +0.48 K; `tp` vs `pr`
  r 0.38 (0.67 shifted one day, because the day windows differ); `ws10` vs `vs` r 0.49, and ERA5 wind is much
  weaker (1.70 vs 3.07 m/s). **Do not mix wind sources without calibration.** The CDS runs one job per user, so
  the 2015–2026 history takes many hours.
- The SMAP ↔ LFMC correlations seen so far (0.70 at lag 0, 0.58 at lag 60 days) are pooled. They do not show
  that soil moisture leads LFMC.

### Required attribution

Generated from the configs. The full terms, verbatim notes and check dates are in `uv run fuellake licenses
--markdown`. Datasets marked `no` must not be republished; derived items inherit that through
`fuellake:redistributable`. OlmoEarth outputs and the Rao et al. LFMC maps are excluded because their terms
conflict with the Space Apps rules. Do not imply endorsement by NASA, NOAA, ESA, the EU or any provider.

| Dataset | Licence | Redistributable | Attribution to include |
|---|---|---|---|
| `biomass` | unknown | no | fuel-lake team biomass subsystem |
| `cop-dem` | [Copernicus-WorldDEM-30-Licence](https://s3.waw3-1.cloudferro.com/swift/v1/portal_uploads_prod/CSCDA_ESA_Mission-specific_Annex_31_Oct_22_latest.pdf) | yes | produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved |
| `era5-land-daily` | [CC-BY-4.0](https://spdx.org/licenses/CC-BY-4.0) | yes | Contains modified Copernicus Climate Change Service information [year]. Munoz Sabater, J. (2019): ERA5-Land hourly data from 1950 to present. C3S CDS, doi:10.24381/cds.e2161bac |
| `features-cube` | various | no | Derived from the input datasets listed in the item property fuellake:source_ids; each input keeps its own licence (fuellake:input_licenses). |
| `fems-lfmc` | unknown | no | USDA Forest Service, FEMS Field Sample database (https://fems.fs2c.usda.gov/) |
| `fire-ignitions` | [other](https://doi.org/10.2737/RDS-2013-0009.7) | no | Short, Karen C. 2026. Spatial wildfire occurrence data for the United States, 1992-2024 [FPA_FOD_20260615]. 7th Edition. Fort Collins, CO: Forest Service Research Data Archive. https://doi.org/10.2737/RDS-2013-0009.7; years after 2024: NIFC Wildland Fire Incident Locations (WFIGS, IRWIN) |
| `fire-perimeters` | [other](https://www.arcgis.com/home/item.html?id=c3c10388e3b24cec8a954ba10458039d) | no | CAL FIRE FRAP California Fire Perimeters (all), with USFS Region 5, BLM, NPS and USFWS; current-year perimeters: NIFC WFIGS Interagency Fire Perimeters |
| `firms` | [other](https://www.earthdata.nasa.gov/faq/firms-faq) | no | We acknowledge the use of data from NASA's Fire Information for Resource Management System (FIRMS) (https://www.earthdata.nasa.gov/firms), part of NASA's Earth Science Data and Information System (ESDIS). |
| `fuel-state` | unknown | no | fuel-lake team, System A |
| `gedi-l4b` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | Dubayah, R.O. et al. (2023) GEDI L4B Gridded Aboveground Biomass Density, Version 2.1. ORNL DAAC, Oak Ridge, Tennessee, USA. doi:10.3334/ORNLDAAC/2299 |
| `globe-lfmc` | [CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) | yes | Yebra et al. (2024) Globe-LFMC 2.0, Scientific Data, doi:10.1038/s41597-024-03159-6; data figshare doi:10.6084/m9.figshare.25413790.v1 |
| `gridmet` | [CC0-1.0](http://creativecommons.org/publicdomain/zero/1.0/) | yes | Abatzoglou, J.T. (2013) Development of gridded surface meteorological data for ecological applications and modelling. Int. J. Climatol. 33:121-131, doi:10.1002/joc.3413 |
| `hrrr-gusts` | [other](https://registry.opendata.aws/noaa-hrrr-pds/) | yes | NOAA/NCEP High-Resolution Rapid Refresh (HRRR) model data from the NOAA Open Data Dissemination program (s3://noaa-hrrr-bdp-pds); daily sampled maxima derived by fuel-lake (modified data) |
| `landfire-canopy` | [CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) | yes | LANDFIRE 2025 Forest Canopy Cover (CC), Canopy Height (CH), Canopy Base Height (CBH) and Canopy Bulk Density (CBD) CONUS, LANDFIRE / USGS EROS, doi:10.5066/P1MAAYLC |
| `landfire-evt` | [US-Public-Domain](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) | yes | LANDFIRE Existing Vegetation Type (EVT) CONUS, LF 2016 Remap, LF 2022 (doi:10.5066/P974JF8W) and LF 2025 (doi:10.5066/P1MAAYLC), LANDFIRE / USDA Forest Service and US Department of the Interior, USGS EROS; class shares derived by fuel-lake (modified) |
| `landfire-fbfm40` | [US-Public-Domain](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) | yes | LANDFIRE Scott and Burgan Fire Behavior Fuel Model (FBFM40) CONUS, LF 2016 Remap, LF 2022 (doi:10.5066/P974JF8W) and LF 2025 (doi:10.5066/P1MAAYLC), LANDFIRE / USDA Forest Service and US Department of the Interior, USGS EROS |
| `landfire-fuel-load` | [US-Public-Domain](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) | yes | Derived from LANDFIRE FBFM40 (LF 2016 Remap, LF 2022 doi:10.5066/P974JF8W, LF 2025 doi:10.5066/P1MAAYLC; LANDFIRE / USGS EROS; the release of each item is in fuellake:landfire_version) with fuel model parameters from Scott, J.H. and Burgan, R.E. (2005) Standard fire behavior fuel models, USDA Forest Service RMRS-GTR-153, Table 7, doi:10.2737/RMRS-GTR-153 |
| `landfire-topo` | [US-Public-Domain](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits) | yes | LANDFIRE 2020 Elevation (Elev), Slope Degrees (SlpD) and Aspect (Asp) CONUS, LANDFIRE / USGS EROS; elevation from the USGS 3D Elevation Program (The National Map) |
| `matchups` | various | no | Derived from the input datasets listed in the item property fuellake:source_ids; each input keeps its own licence (fuellake:input_licenses). |
| `nisar-gcov` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | NISAR data courtesy of NASA/JPL-Caltech and ISRO, distributed by the ASF DAAC (doi:10.5067/NIL2GCOV-P1) |
| `nisar-sme2` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | NISAR soil moisture courtesy of NASA/JPL-Caltech and ISRO, distributed by the ASF DAAC (doi:10.5067/NIL3SME2-P1) |
| `opera-rtc-s1` | [CC0-1.0 AND Copernicus-Sentinel-legal-notice](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | OPERA RTC-S1 (NASA/JPL-Caltech, ASF DAAC, doi:10.5067/SNWG/OPERA_L2_RTC-S1_V1). Contains modified Copernicus Sentinel data 2026 |
| `point-features` | various | no | Derived from the input datasets listed in the item property fuellake:source_ids and the remote sources in fuellake:remote_sources; each input keeps its own licence (fuellake:input_licenses). |
| `sentinel2-indices` | [Copernicus-Sentinel-Data-Legal-Notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice) | yes | Contains modified Copernicus Sentinel data 2026 |
| `smap-climatology` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | Derived from SMAP L4 SPL4SMGP v008 (Reichle et al. 2025, doi:10.5067/T5RUATAQREF8) and SMAP L3 SPL3SMP_E v006 (O'Neill et al. 2023, doi:10.5067/M20OXIZHY3RJ), NASA NSIDC DAAC |
| `smap-l3e` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | O'Neill, P. E., Chan, S., Njoku, E. G., Jackson, T., Bindlish, R., Chaubell, J. & Colliander, A. (2023). SMAP Enhanced L3 Radiometer Global and Polar Grid Daily 9 km EASE-Grid Soil Moisture (SPL3SMP_E, Version 6). NASA NSIDC DAAC. doi:10.5067/M20OXIZHY3RJ |
| `smap-l4` | [CC0-1.0](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance) | yes | Reichle, R., De Lannoy, G., Koster, R. D., Crow, W. T., Kimball, J. S., Liu, Q. & Bechtold, M. (2025). SMAP L4 Global 3-hourly 9 km EASE-Grid Surface and Root Zone Soil Moisture Geophysical Data (SPL4SMGP, Version 8). NASA NSIDC DAAC. doi:10.5067/T5RUATAQREF8 |
| `spread-risk` | unknown | no | fuel-lake team, System B |

Extra notices: Copernicus DEM redistributors must add "The organisations in charge of the Copernicus programme by
law or by delegation do not incur any liability for any use of the Copernicus WorldDEM-30". HRRR daily maxima are
modified NOAA data: say so when you share them. The region boundary is "U.S. Census Bureau, 2025 Cartographic
Boundary File cb_2025_us_state_500k".

## Getting data out

Four ways, from most to least abstract. On 2026-11-14 the Python, HTTP (curl and pystac-client) and
direct-file snippets, the publish examples and one pass of the event loop were executed against copies of two
development lakes. `open_cube` was run on `ca`, not `smm`. From the CLI block, `datasets`, `licenses`, `discover`,
`events` and `validate` were executed. The `run gridmet`, `build-points` and `build-features` lines were only
checked against the CLI parser (via the `full_run.sh` dry run), not executed.

### 1. CLI

```bash
uv run fuellake datasets                                # what exists (config only; --json for JSON)
uv run fuellake licenses --markdown                     # licences and attributions
uv run fuellake discover fire-ignitions --aoi ca --start 2024-01-01 --end 2025-12-31
#   = already cataloged, + new; asks the source, writes nothing
uv run fuellake run gridmet --aoi ca --start 2026-10-01 --end 2026-10-31    # ETL; re-running is a no-op
uv run fuellake events --since 2026-11-14 --type item.created              # JSON lines
uv run fuellake validate fire-ignitions point-features fuel-state          # contracts + region; exit 1 on error
uv run fuellake build-points --aoi ca --start 2016-01-01 --end 2024-12-31 --s2-max-points 5000
uv run fuellake build-features --aoi smm --start 2026-06-01 --end 2026-11-14 --what all   # or cube / matchups
uv run fuellake serve --port 8000                       # read-only HTTP API
```

There is no `search` subcommand: `discover` asks the *source*, and catalog search is `LakeClient.search` or
`/search` in the API. Flags of `run`: `--force` (reprocess), `--limit N`, `--force-scale` (run a local dataset on
`ca`) and `--json`. `run` with no ids (or `all`) skips stubs, scale mismatches and units whose credential is
missing. A dataset named explicitly always runs and fails loudly. All options: `uv run fuellake <cmd> --help`.

### 2. Python `LakeClient`

```python
from fuellake.client import LakeClient

client = LakeClient.from_env()     # direct mode on $LAKE_ROOT; HTTP mode if $FUELLAKE_API_URL is set
# or: LakeClient("/data/fuel-lake"), LakeClient("s3://bucket/prefix"), LakeClient(api_url="http://host:8000")

items = client.search(collections=["gridmet"], aoi="ca", start="2026-10-01", end="2026-10-31")
vpd = client.open_asset(items[0], "vpd")               # form B: DataArray (time, y, x), native 4 km grid
fuel = client.search(collections=["landfire-fbfm40"], aoi="smm")         # static layers: no time filter needed
fm = client.open_asset(fuel[-1], "fuel_model")          # form A: 2-D (y, x) COG with CRS (fm.rio.crs)

lfmc = client.read_table("lfmc_samples", sources=["fems"], start="2026-06-01", end="2026-11-14")
ign = client.read_table("fire_ignitions")               # deduped on ignition_id
train = ign[ign["fire_year"].between(2016, 2024)]       # no `date` column: filter in pandas, not start=/end=
perims = client.read_table("fire_perimeters")           # polygons; columns fire_year, start_date, area_acres...
pts = client.read_table("point_features")               # gold, from `fuellake build-points`
m = client.read_table("matchups")                       # gold, from `fuellake build-features`
cube = client.open_cube("smm")                          # gold datacube, xarray.Dataset (lazy, dask)
```

Results on the dev lakes: `ign` had 96,818 rows, `train` 74,568, of which 477 were ≥ 400 ha (0.64 %); `vpd` was
(31, 231, 249); `fm` (837, 1398) in EPSG:32611. The `start`/`end` arguments of `read_table` filter a `date` column
and raise `KeyError` on tables without one (`fire_ignitions`, `fire_perimeters`). `search` refuses a NISAR series
that mixes maturities: pass `query={"fuellake:maturity": "PROVISIONAL"}`. Other methods are `get_item`,
`events`, `collections`, `datasets`, `publish_raster` and `publish_table`.

### 3. HTTP API (`fuellake serve`, read-only STAC-API-lite)

```bash
LAKE_ROOT=/data/fuel-lake uv run fuellake serve --host 127.0.0.1 --port 8000   # OpenAPI docs at /docs
curl -s localhost:8000/health
curl -s localhost:8000/collections
curl -s "localhost:8000/search?collections=fuel-state&limit=5"
curl -s -X POST localhost:8000/search -H 'Content-Type: application/json' \
     -d '{"collections":["spread-risk"],"aoi":"smm","datetime":"2026-10-17/2026-10-17"}'
curl -s -o lfmc.tif localhost:8000/assets/fuel-state/fuel-state__smm__20261015/lfmc   # one COG
curl -s -o ign.parquet localhost:8000/tables/fire_ignitions      # ?sources=&start=&end=&dedupe=false
curl -s "localhost:8000/events?since=2026-11-14T00:00:00Z&type=item.created"
```

```python
import pystac_client
cat = pystac_client.Client.open("http://127.0.0.1:8000")
for item in cat.search(collections=["gridmet"], datetime="2026-10-01/2026-10-31").items():
    print(item.id, item.assets["data"].href)
```

The API also serves `/collections/{id}/items[/{item}]`, `/datasets`, `/cubes/{aoi}` and Zarr sub-paths under
`/assets/...`. It has no authentication and serves non-redistributable data, so keep it on localhost or a
private network (SSH tunnel: `ssh -L 8000:localhost:8000 server`).

### 4. Direct files

```
raw/<dataset>/<yyyy>/<mm>/...                        source subsets as fetched
staged/<dataset>/<aoi>/<item_id>/<variable>.tif      form A COG on the AOI grid
staged/<dataset>/<aoi>/<item_id>/data.zarr           form B Zarr v3, native grid, (time, y, x)
staged/tables/<table>/<dataset>/part-<key>.parquet   GeoParquet 1.1, EPSG:4326, one part per unit and AOI
features/<aoi>/cube.zarr | matchups.parquet | point_features.parquet     gold
catalog/catalog.json, catalog/<collection>/<item_id>/<item_id>.json        static STAC (relative hrefs)
events/<yyyy-mm-dd>.jsonl    runs/<run_id>.json    inbox/biomass/
```

```python
import glob, geopandas as gpd, pandas as pd, rasterio, xarray as xr
LAKE = "/data/fuel-lake"
parts = sorted(glob.glob(f"{LAKE}/staged/tables/fire_ignitions/fire-ignitions/part-*.parquet"))
ign = pd.concat([gpd.read_parquet(p) for p in parts]).drop_duplicates("ignition_id")  # parts repeat per AOI
with rasterio.open(f"{LAKE}/staged/landfire-fbfm40/smm/landfire-fbfm40__smm__20250101__lf2025/fuel_model.tif") as src:
    fm = src.read(1)
ds = xr.open_zarr(f"{LAKE}/staged/gridmet/ca/gridmet__ca__20261001/data.zarr", consolidated=False)
pts = gpd.read_parquet(f"{LAKE}/features/ca/point_features.parquet")
```

Prefer the client: it deduplicates parts, applies the maturity rule and works on `s3://` lakes. With direct
files you must dedupe yourself.

## Guide for System A (fuel state)

**Goal:** maps of flammable dry fuel load and LFMC, plus their combined effect on spread, published to
collection **`fuel-state`**.

### Inputs in the lake

| Signal | Dataset (variables) | Where | Use / caveat |
|---|---|---|---|
| Biomass label / prior | `gedi-l4b` (`agbd`, `agbd_se`, `quality_flag`) | `ca`, `smm` | 1 km, 2019-04..2023-03, live AGB, not surface fuel; coarse for chaparral |
| Nominal fuel loads | `landfire-fuel-load` (`total_load`, `dead_*`, `live_*`, `fuel_bed_depth`) | `smm`; remote per point | Scott & Burgan table values per fuel model |
| Fuel model | `landfire-fbfm40` (`fuel_model`) by release | `smm`; remote per point | choose the release by date (below) |
| Canopy | `landfire-canopy` | `smm` | forest only; 0 in chaparral |
| Vegetation state | `sentinel2-indices` (`ndvi`, `ndmi`) | `smm`; `s2_*` in `point_features` | NDMI tracks canopy water |
| L-band cross-pol | `nisar-gcov` (`gamma0_hv_db`) | `smm` | search-only: download untested on real data; HH+HV since 2026-06 |
| Soil moisture (leading signal) | `nisar-sme2` (`soil_moisture`, DSG) | `ca`, `smm` | 2026 only, PROVISIONAL, +0.11 wet bias → anomalies |
| Soil moisture (history) | `smap-l4` (`sm_surface`, `sm_rootzone`, `smap_sm`), `smap-l3e` | `ca`, `smm` | 9 km; L4 gap-free since 2015 |
| Drying drivers | `gridmet` (`vpd`, `tmmx`, `pr`, `pet`, `fm100`, `fm1000`), `era5-land-daily` (`t2m_max`, `d2m`, `tp`, `swvl1`–`swvl4`) | `ca`, `smm` | lags are single days |

### Labels

- **LFMC**: `client.read_table("lfmc_samples")`, with `source` `globe-lfmc` (Yebra 2024, CC0, 1977–2023,
  scientific species names) or `fems` (USDA FEMS, current, common names). The sources overlap for 2017–2023. The
  simplest rule is Globe-LFMC up to 2023 and FEMS from 2024. Screen with `qc_flags` and `outlier_flag`. Values are
  in % of dry weight.
- **Biomass**: GEDI L4B `agbd` (Mg/ha; 1 Mg/ha = 0.1 kg/m²). Drops from the team's biomass subsystem go to
  `inbox/biomass/` (contract in `config/datasets/biomass.yaml`).

### Training rows

- **Statewide, per point (recommended):** use the one table that `scripts/full_run.sh` builds
  (`fuellake build-points --aoi ca --start 2016-01-01 --end 2024-12-31`, ignitions and LFMC samples together) and
  filter `point_type == "lfmc_sample"`. **Do not rebuild it with `--tables lfmc_samples`:** a build overwrites
  `features/ca/point_features.parquet` and replaces the AOI's single item, so it would delete System B's rows.
  The table is **causal by default**: weather on
  `date − L` for `--lags-days 0,15,30,45,60,90`, scenes on or before the date, and the static release chosen per
  sample (below). It adds `sme2_anomaly`/`sme2_zscore`, `block_50km` and `missing_inputs` (missing values are null,
  never 0). **The `sme2_*` columns are null for all 2016–2024 rows**, because SME2 starts in 2026. To use SME2,
  both systems must agree on one rebuild whose period reaches 2026 (FEMS samples, WFIGS ignitions) once SME2 is
  staged, because there is one table per AOI and each build replaces it. Sentinel-2 at every point is slow (about 20 point-dates per minute from a laptop, 20–60 h for all
  ignitions); cap it with `--s2-max-points` or use `--no-sentinel2`.
- **On the 30 m `smm` grid:** `fuellake build-features --aoi smm --what matchups` gives one row per LFMC sample in
  the `smm` bbox. It uses Sentinel-2/GCOV pixels in a k × k window and a nearest scene within `--window-days`.
  ⚠ The cube and matchups pick **one static release per build** (the latest on or before `--end`), not one per
  sample. For periods that span LANDFIRE releases, use `point_features` or build one period per release.
- **Accumulations** (lag columns are single days): compute them from the daily form B series.

```python
import xarray as xr
items = client.search(collections=["gridmet"], aoi="ca", start="2026-09-01", end="2026-10-31")
pr = xr.concat([client.open_asset(i, "pr") for i in items], dim="time")
pr72 = pr.rolling(time=3).sum()                                    # 72 h precipitation, mm
tmax_c = xr.concat([client.open_asset(i, "tmmx") for i in items], dim="time") - 273.15
gdd = (tmax_c - 10.0).clip(min=0).cumsum("time")                  # growing degree days (Tmax, base 10 C)
```

### Spatial validation

Rows are not independent (sites, species, repeat dates). Split by **`block_50km`**: 250 × 250 SME2 cells
(50.045 km) counted from the EASE-Grid 2.0 corner; every `ca` and 1 km GEDI cell nests in one block. Report the
out-of-block error next to a random-split error; a random split is optimistic.

```python
import numpy as np
lf = pts[pts.point_type == "lfmc_sample"].dropna(subset=["lfmc_pct", "vpd", "sm_surface", "tmmx"])
X = np.c_[np.ones(len(lf)), lf[["vpd", "sm_surface", "tmmx"]].to_numpy()]; y = lf["lfmc_pct"].to_numpy()
rng = np.random.default_rng(0); blocks = lf["block_50km"].to_numpy(); ub = np.unique(blocks)
block_fold = dict(zip(ub, rng.integers(0, 5, len(ub))))
def cv_rmse(fold):
    sq = []
    for k in range(5):
        tr, te = fold != k, fold == k
        coef, *_ = np.linalg.lstsq(X[tr], y[tr], rcond=None)          # replace with your model
        sq.append((X[te] @ coef - y[te]) ** 2)
    return float(np.sqrt(np.concatenate(sq).mean()))
print(cv_rmse(np.array([block_fold[b] for b in blocks])), cv_rmse(rng.integers(0, 5, len(y))))
```

On the 2,246-row dev table this printed 37.3 % (out-of-block) against 36.7 % (random) with a toy linear model.
scikit-learn's `GroupKFold(groups=block_50km)` does the same, but it is not a project dependency.

### SME2 anomaly against the SMAP climatology

`point_features` already carries `sme2_anomaly = SME2 − l4_sm_surface_mean` and
`sme2_zscore = anomaly / l4_sm_surface_std`. Both are taken on the month and day of the SME2 scene and
interpolated from the 9 km climatology. The +0.11 bias is not removed, so it is a constant offset in the anomaly.
For maps:

```python
from datetime import date
from fuellake.connectors.smap import climatology_step
clim = client.search(collections=["smap-climatology"], aoi="ca")[0]     # no date filter: dated 1904
sme2 = client.search(collections=["nisar-sme2"], aoi="ca", start="2026-09-01", end="2026-09-12",
                     query={"fuellake:maturity": "PROVISIONAL"})[0]
step = climatology_step(date.fromisoformat(sme2["properties"]["datetime"][:10]))
mean = client.open_asset(clim, "l4_sm_surface_mean").isel(time=step)   # 9 km: regrid to the 200 m ca grid
sm = client.open_asset(sme2, "soil_moisture")                          # then anomaly = sm - mean_on_ca_grid
```

(The SME2/climatology snippet was **not executed**: no climatology item exists until the server run.)

### Output contract: `fuel-state` (from `config/datasets/fuel-state.yaml`)

| Variable | Unit | Valid range | Required | Meaning |
|---|---|---|---|---|
| `fuel_load` | kg/m² | 0–100 | yes | oven-dry flammable fuel (live + dead surface) per ground area |
| `lfmc` | % | 0–400 | yes | live fuel moisture, % of dry weight |
| `spread_potential` | 1 | 0–1 | yes | relative potential of the fuel alone (no weather, no terrain) to carry fire; monotone in amount and dryness; not a probability |
| `fuel_load_p05`, `fuel_load_p95` | kg/m² | 0–100 | no | uncertainty band |
| `lfmc_p05`, `lfmc_p95` | % | 0–400 | no | uncertainty band |

Properties: `fuel_state:model_version` (required) and `fuellake:source_items` (filled from `source_items`).
Arrays must be exactly on the AOI grid (`load_aoi(aoi).grid`); there is no silent reprojection. A value outside
its range is **rejected**, not clipped, and pixels outside California become NaN.

```python
import numpy as np
from fuellake.client import LakeClient
from fuellake.registry import load_aoi

client = LakeClient("/data/fuel-lake")                 # publishing needs direct mode (path or s3://)
grid = load_aoi("smm").grid                            # (837, 1398) EPSG:32611; ca: (4827, 4956) EPSG:6933
inputs = (client.search(collections=["landfire-fbfm40"], aoi="smm")[-1:]
          + client.search(collections=["gridmet"], aoi="ca", start="2026-10-01", end="2026-10-31"))
fuel_load = np.full(grid.shape, 1.2, dtype="float32")                    # your model output, kg/m2
lfmc = np.full(grid.shape, 85.0, dtype="float32")                        # %
spread_potential = np.clip((150.0 - lfmc) / 150.0, 0, 1).astype("float32")
fs = client.publish_raster(
    "fuel-state", "smm", "2026-10-15",
    {"fuel_load": fuel_load, "lfmc": lfmc, "spread_potential": spread_potential,
     "lfmc_p05": lfmc - 15, "lfmc_p95": lfmc + 15},
    properties={"model_version": "fuelstate-gbm-0.1"},
    source_items=inputs)                               # -> item fuel-state__smm__20261015, event item.created
```

Publishing the same dataset, AOI and date again replaces the item (`item.updated`). Use
`discriminator="v2"` to keep both.

## Guide for System B (fire-spread risk)

**Goal:** a fire-spread risk map for a given area and validity window, published to collection
**`spread-risk`**.

### Inputs

- **Fuel state:** `latest = client.search(collections=["fuel-state"], aoi="smm")[-1]`, then
  `client.open_asset(latest, "lfmc")` and `"spread_potential"`, `"fuel_load"`.
- **Weather (`gridmet`, daily, about 2 days latency):** `vs` (wind), `th` (wind direction, circular), `rmin`,
  `vpd`, `fm100`/`fm1000` (dead fuel moisture), `pr` (with 72 h sums computed as above), `erc` and `bi`.
  **`hrrr-gusts`** gives `gust_max`/`wind10_max` on `smm` (sampled, lower bounds). `era5-land-daily` gives
  `t2m`, `t2m_max`, `d2m`, `tp`, `ws10`, `ws10_max` and `swvl1`–`swvl4`; its wind is much weaker than
  gridMET's, so pick one wind source.
- **No weather forecast is in the lake.** Everything above is an analysis or reanalysis of past days. A
  forward-looking map needs a forecast source (e.g. HRRR forecast hours or NWS NDFD), which is **a gap to fill**.
  Until then, state the weather actually used in `spread_risk:weather_source`.
- **Terrain:** `landfire-topo` (`elevation`, `slope`, `aspect` from true north; preferred) or `cop-dem` (a DSM).
- **Fuels:** `landfire-fbfm40` and `landfire-fuel-load`, plus canopy for crown fire.

### Labels

- **`fire_ignitions`**: model **P(fire ≥ 400 ha | ignition)**. The label is `large_fire` in `point_features`, or
  `fire_size_ha >= 400`. There are 477 positives out of 74,568 ignitions in 2016–2024 (0.64 %): use PR-AUC and
  calibration, not accuracy. **Train on 2016–2024 only** (see the WFIGS caveats above). `cause_class` lets you
  separate human from natural ignitions.
- **`fire_perimeters`**: final burned extents (FRAP), for spread extent and validation maps. **`firms`** gives
  active-fire detections (use `detection_type == 0`) for fire progression.
- Rows: the shared `point_features` table from `scripts/full_run.sh`, filtered on `point_type ==
  "fire_ignition"` (label `large_fire`). Do not rebuild it with `--tables fire_ignitions`: that replaces the
  whole table, LFMC rows included.

### Pre-fire LANDFIRE release (avoid post-fire leakage)

A LANDFIRE release includes the disturbances of its own year, so a later release shows the burn scar (see
Woolsey above). `build-points` therefore picks per point **the latest release whose disturbance window ended
before the ignition**: LF 2016 counts from 2017-01-01, LF 2022 from 2022-10-01 and LF 2025 from 2025-10-01 (GEDI
from 2023-03-17). Terrain is exempt. Consequences:

- **2016 ignitions get no fuel data** (`missing_inputs` contains `fuel:no_release`);
- 2017 to September 2022 ignitions use LF 2016;
- the release switch is also a mapping change, so keep `fuel_release` as a covariate.

The `fuel_release` and `fuel_source` columns record the choice. The cube and matchups do **not** do this per
sample (one release per build). Earlier or intermediate releases (LF 2014, LF 2019/2020, LF 2023, LF 2024) would
fill the gaps; LF 2023 and LF 2024 services exist but are not staged.

### Baseline to beat: Burning Index

gridMET `bi` (NFDRS fuel model G) on the ignition date is the operational index. Report the AUC and PR-AUC of
`bi` against `large_fire` next to your model, using the same spatial blocks.

```python
def auc(score, label):                           # rank AUC, no extra dependency
    r, pos = score.rank().to_numpy(), label.to_numpy(bool)
    n1, n0 = pos.sum(), (~pos).sum()
    return float((r[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))
ign = pts[(pts.point_type == "fire_ignition") & pts.large_fire.notna()].dropna(subset=["bi"])
print(auc(ign["bi"], ign["large_fire"]), ign["large_fire"].mean())
```

(This was executed on synthetic rows; the dev point table held only LFMC rows.)

### Output contract: `spread-risk` (from `config/datasets/spread-risk.yaml`)

| Variable | Unit | Range | Required | Meaning |
|---|---|---|---|---|
| `risk_index` | 1 | 0–1 | yes | predicted spread risk over the validity window |
| `risk_class` | class (uint8) | 1–5, 0 = nodata | yes | **class breaks to be defined by the team** and stated with the model version |
| `rate_of_spread` | m/min | 0–1000 | no | head-fire rate of spread |
| `risk_p05`, `risk_p95` | 1 | 0–1 | no | uncertainty band |

Required properties: `spread_risk:valid_from` and `spread_risk:valid_to` (they become `start_datetime` and
`end_datetime`), `spread_risk:model_version`, `spread_risk:weather_source`, and `spread_risk:fuel_state_item`
(filled automatically when exactly one fuel-state item is in `source_items`).

```python
latest = client.search(collections=["fuel-state"], aoi="smm")[-1]
sp = client.open_asset(latest, "spread_potential").values
wx = client.search(collections=["gridmet"], aoi="ca", start="2026-10-15", end="2026-10-15")
bi = client.open_asset(wx[0], "bi").sel(time="2026-10-15")        # 4 km; regrid to the AOI grid yourself
risk_index = np.nan_to_num(sp, nan=0.0).astype("float32")         # your model here
risk_class = (np.digitize(risk_index, [0.2, 0.4, 0.6, 0.8]) + 1).astype("uint8")   # placeholder breaks
risk = client.publish_raster(
    "spread-risk", "smm", "2026-10-15T18:00:00Z", {"risk_index": risk_index, "risk_class": risk_class},
    properties={"valid_from": "2026-10-16T00:00:00Z", "valid_to": "2026-10-18T00:00:00Z",
                "model_version": "spreadrisk-0.1", "weather_source": "gridmet"},
    source_items=[latest, *wx])
# -> spread-risk__smm__20261015__180000; a search with start/end matches the validity window
```

### Reacting to new `fuel-state` items (microservice loop)

```python
import time
seen, since = set(), "2026-11-14T00:00:00Z"
while True:
    for ev in client.events(since=since, types=["item.created", "item.updated"]):   # oldest first, inclusive
        since = ev["time"]
        if ev.get("collection") == "fuel-state" and ev["event_id"] not in seen:
            seen.add(ev["event_id"])
            item = client.get_item(ev["collection"], ev["item_id"])
            ...                                   # recompute risk for item["properties"]["fuellake:aoi"]
    time.sleep(300)
```

Events work the same in HTTP mode (`GET /events?since=&type=`). Publishing needs a direct-mode client on the
same lake root, and only one process should write at a time. More patterns are in
[docs/microservices.md](docs/microservices.md) and `src/fuellake/services/example_consumer.py`.

## Running on a server

### Requirements

| | |
|---|---|
| OS / tools | Linux or macOS, `git`, [uv](https://docs.astral.sh/uv/) (it installs Python 3.12; no system GDAL needed) |
| Size | about 16 vCPU, 64 GB RAM, 500 GB disk (the measured data are tens of GB; the rest is headroom for cubes and caches) |
| Location | AWS **us-west-2** is next to NASA Earthdata Cloud (SMAP, NISAR, GEDI, OPERA) and the AWS Open Data buckets (Sentinel-2, Copernicus DEM, HRRR) |
| Network | outbound HTTPS to NASA Earthdata, NKN THREDDS (gridMET), LANDFIRE LFPS, ArcGIS Online, USDA FEMS, figshare, FIRMS, CDS |

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <repo-url> fuel-lake && cd fuel-lake && uv sync
uv run pytest -q -m "not network and not earthdata"     # offline suite, about 1 minute
```

### Credentials

| Variable | Needed for | Without it |
|---|---|---|
| `EARTHDATA_TOKEN` | `nisar-sme2`, `nisar-gcov`, `opera-rtc-s1`, `gedi-l4b`, `smap-l4`, `smap-l3e` (and so `smap-climatology`) | these are skipped |
| `FIRMS_MAP_KEY` | `firms` for the current year | past years come from the keyless archive |
| `CDSAPI_URL`, `CDSAPI_KEY` | `era5-land-daily` (accept the licence in the CDS account first) | skipped |

Keep the credentials in environment variables or in a private file **outside the repo**, e.g.
`~/.config/fuel-lake/secrets.env` (`chmod 600`, lines `KEY=value`). **Never commit them** and never paste them
into logs or issues. fuel-lake reads them only when needed and redacts them from errors. Rotate them after the
hackathon. Check Earthdata access with `uv run python scripts/probe_earthdata.py`.

### `scripts/full_run.sh`

```bash
export LAKE_ROOT=/data/fuel-lake
DRY_RUN=1 ENV_FILE=~/.config/fuel-lake/secrets.env scripts/full_run.sh        # print the plan
ENV_FILE=~/.config/fuel-lake/secrets.env nohup scripts/full_run.sh > ~/full_run.out 2>&1 &
tail -f ~/fuel-lake-logs/full_run_*.log
```

The script runs sequentially (single writer; gridMET never runs in parallel), in this order:

1. `init`, then a report of which credential variables are set (names only).
2. **Region `ca`, training inputs:**
   - labels: `globe-lfmc` 1977–2023, then `fems-lfmc`, `fire-ignitions`, `fire-perimeters` and `firms` (current
     year only with `FIRMS_MAP_KEY`);
   - `gridmet`, `smap-l4` and `smap-l3e`;
   - `smap-climatology` (after the whole 2015-10..2024-12 baseline);
   - `gedi-l4b`;
   - `landfire-evt` (vegetation class shares on the 200 m grid; 60 blocks of 30 m clips per release).
3. **`build-points --aoi ca`** for 2016-01-01..2024-12-31 (`--s2-max-points $S2_MAX_POINTS`). It needs neither
   SME2 nor ERA5 for these years, so the training table is ready early.
4. **`nisar-sme2` on `ca`** for 2025 onward, with **both pass directions**, through a config overlay with
   `region_orbit_state: null`.
5. **Local `smm`:**
   - `landfire-topo`, `cop-dem`, `landfire-fbfm40`, then `landfire-fuel-load` (it needs the FBFM40 items),
     `landfire-canopy` and `landfire-evt`;
   - `gedi-l4b`, plus `gridmet`, `smap-l4`, `smap-l3e`, `sentinel2-indices` and `hrrr-gusts` (from `HRRR_START`):
     the smm cube needs its own form B items;
   - `nisar-sme2` (`--force` once, so old items get the current status), `nisar-gcov` and `opera-rtc-s1` (2025
     onward).
6. **`era5-land-daily`** on both AOIs: current period only, and the 2015–2024 history only with `ERA5_HISTORY=1`
   (the CDS runs one job per user).
7. **Gold and checks:** `build-features --aoi smm --what matchups` for START..END, `--what cube` from
   `CUBE_START`, and finally `validate`.

Each period is run as training (`START`..`TRAIN_END`, default 2015-10-01..2024-12-31), then current
(`CURRENT_START`..`END`, default 2025-01-01..today). Parameters: `START`, `END`, `TRAIN_END`, `CURRENT_START`,
`AOIS` (default `"ca smm"`), `SKIP` (e.g. `SKIP="hrrr-gusts gridmet@smm build-points"`), `POINTS_START`,
`POINTS_END`, `S2_MAX_POINTS` (default 5000; `all` or `0`), `CUBE_START` (default 2026-06-01), `HRRR_START`
(default 2025-01-01), `SME2_ORBITS` (`both` | `descending`), `ENV_FILE`, `LOG_DIR` (default `~/fuel-lake-logs`) and
`ERA5_HISTORY=1` and `DRY_RUN=1`.

The script's real execution path was run once (on `smm`, with `init`, `landfire-topo` and `validate`, the other
steps skipped); the full plan was checked only as a dry run, with every printed command parsed by the CLI. A
failing step is logged and the script goes on. At the end it lists skipped and failed steps and exits 1 if any
step failed. **To resume, run the same command again:** cataloged units are skipped, so only missing or failed
units are fetched. Use `--force` by hand only to reprocess. Run manifests are in `<lake>/runs/` and events in
`<lake>/events/`.

### Expected sizes and durations (from the agents' runs on 2026-11-14; extrapolations marked ~)

| Dataset | Measured | Full run (estimate) |
|---|---|---|
| `gridmet` `ca` | 63 months staged and raw ≈ 1.0 GB in a dev lake; ~30.7 MB per month with 11 variables | ~4 GB for 2015-10..2026-11; hours (polite, serial per variable) |
| `smap-l4` `ca` | 134 months, 463 MB subsets (about 110 KB per day) | done in about the same size |
| `smap-l3e` `ca` | 2 months, 4.5 MB | ~0.25 GB, ~1 h |
| `smap-climatology` | — | minutes, no download |
| `nisar-sme2` `ca` | 27 s per statewide granule; 13 MB download, 33 MB disk | 2026-06..11: ~518 granules (both directions) ≈ 7 GB download, 17 GB disk, ~4 h |
| `fire-ignitions` `ca` | 55 MB raw, 9 MB staged, 11 items | minutes |
| `globe-lfmc` | 72 MB workbook (cached outside the lake) | minutes |
| `gedi-l4b` `ca` | 29 MB | minutes |
| `hrrr-gusts` `smm` | 795 MB for 2026-06..11 | ~140 MB per month |
| LANDFIRE / DEM `smm` | 1–10 MB per item | minutes |
| `build-points` `ca` | 2,246 rows in 5 s without Sentinel-2; remote statics ~10 min on the first run (SQLite cache) | Sentinel-2 ~20 point-dates/min on a laptop: 5,000 ≈ 4 h, all ≈ 20–60 h |
| `era5-land-daily` | one month on `ca`; ~4–6 min per month per variable group | history 2015–2026: many hours (one CDS job at a time) |

### Docker compose with MinIO (never run)

`docker/compose.yaml` defines MinIO, a bucket init, a one-shot ETL and the API on one image (`docker/Dockerfile`).
**It has never been run** (no Docker on the development machine; only static checks in the tests).

```bash
export MINIO_ROOT_PASSWORD=...                              # required; MINIO_ROOT_USER defaults to fuellake
docker compose -f docker/compose.yaml up -d minio bucket-init api          # API on :8000, lake s3://fuel-lake
FUELLAKE_DATASETS=gridmet FUELLAKE_AOI=ca FUELLAKE_RUN_ARGS="--start 2026-10-01 --end 2026-10-31" \
  docker compose -f docker/compose.yaml run --rm etl
```

For the full run, prefer `scripts/full_run.sh` on the host with a local `LAKE_ROOT` or with
`LAKE_ROOT=s3://...` plus `FUELLAKE_S3_ENDPOINT_URL`.

## Validation status

From the `status` fields and comments in `config/datasets/*.yaml` (2026-11-14). The offline test suite (`uv run
pytest -q -m "not network and not earthdata"`) covers every connector with fixtures; the table says what ran on
**real** data.

| Dataset | Status | Tested on real data | Fixtures / synthetic only, or not yet run |
|---|---|---|---|
| `biomass` | tested | synthetic drops only | drop-zone contract |
| `cop-dem` | tested | `smm` run, rerun no-op, validate OK | — |
| `era5-land-daily` | tested | `ca` 2026-10 (one month) | history 2015–2026 not run (CDS queue) |
| `features-cube` | unverified | `ca` gridMET 2026-10-01..03 | otherwise synthetic lakes |
| `fems-lfmc` | tested | `smm`; `ca` 2026-01..11 (3,547 rows) | older months on `ca` not run |
| `fire-ignitions` | tested | `ca` 2016-01-01..2026-11-14 (96,818 rows) | — |
| `fire-perimeters` | tested | `smm` 2015–2026; `ca` 2020–2026 (2,910 polygons) | `ca` before 2020 not run |
| `firms` | tested | keyless archive, `smm` 2018 and 2024–2025 | MAP_KEY area API (current year) never run; `ca` not run |
| `fuel-state` | interface | demo publish on a lake copy | no real model output yet |
| `gedi-l4b` | tested | `smm` and `ca` | — |
| `globe-lfmc` | tested | `ca` 1977–2023 (48,575 rows, 319 sites) | — |
| `gridmet` | tested | `ca` 63 months (11 variables); `smm` 2026-06..11 (7 variables) | full `ca` 2015-10..2026-11 on the server |
| `hrrr-gusts` | tested | `smm` 2026-06-01..11-13 (795 MB) | `ca` never run |
| `landfire-canopy` | tested | `smm` LF 2025 | `ca` (remote point reads only) |
| `landfire-evt` | tested | `smm` LF 2016, LF 2022, LF 2025 (2026-11-15) | a full `ca` run through the connector (its block arithmetic reproduces three blocks of the rasters staged earlier by a research script) |
| `landfire-fbfm40` | tested | `smm` LF 2016, LF 2022, LF 2025 | `ca` (remote point reads only) |
| `landfire-fuel-load` | tested | `smm`, one item per release | `ca` |
| `landfire-topo` | tested | `smm` LF 2020 | `ca` (remote point reads only) |
| `matchups` | unverified | small real `ca` build | mostly synthetic lakes |
| `nisar-gcov` | search-only | live CMR discovery only | download and transform on synthetic HDF5 only |
| `nisar-sme2` | tested | `smm` 11 granules 2026-06-03..10-13; `ca` 13 descending granules | full `ca` (both directions) on the server |
| `opera-rtc-s1` | search-only | live CMR discovery only | download and transform on synthetic bursts only |
| `point-features` | unverified | small real `ca` build: FEMS 2026-06..11, 2,246 rows | full 2016–2024 ignition build on the server |
| `sentinel2-indices` | tested | `smm`, newest dates of 2026-06..11 (`--limit`) | full history on the server |
| `smap-climatology` | unverified | never built on real data (needs full L3E) | method tested offline |
| `smap-l3e` | tested | `ca` 2015-10..11 only | full record on the server (~0.25 GB, ~1 h) |
| `smap-l4` | tested | `ca` 2015-10..2026-11 (134 months); `smm` partial | — |
| `spread-risk` | interface | demo publish on a lake copy | no real model output yet |

## Known gaps and limitations

- **No weather forecast source.** Risk maps can only use past or analysed weather.
- **NISAR:**
  - SME2 is PROVISIONAL, 2026 only and wet-biased;
  - on `ca` only 13 descending granules were staged locally, so the full both-direction run happens on the
    server;
  - a statewide cube gets one mostly-empty item per granule (12-day compositing is not implemented);
  - over California NISAR records HH+HV only, there is a gap from 2026-07-27 to 08-10, and SME2 is upsampled
    on `smm` (no extra detail);
  - GCOV and OPERA are search-only: their download path was never run on real data.
- **SMAP:** L3E was staged only for 2015-10..11 and the climatology was never built on real data; both happen on
  the server. The `smap_sm` mapping is provisional.
- **LANDFIRE:**
  - no release before LF 2016, so 2016 ignitions have no fuels;
  - LF 2023 and LF 2024 are not staged;
  - releases differ in mapping method;
  - LANDFIRE is not staged on `ca` (remote point reads only);
  - the cube and matchups use one release per build.
- **Labels:**
  - WFIGS 2025–2026 is not comparable with FPA-FOD;
  - Globe-LFMC ends in 2023 and does not overlap NISAR;
  - FEMS overlaps Globe-LFMC for 2017–2023;
  - FEMS has no stated licence.
- **Gold layer:** `point-features`, `features-cube` and `matchups` are `unverified`: they ran on small real or
  synthetic lakes only. Check fill rates (`fuellake:fill_rates`) before training.
- **Coverage:** HRRR, Sentinel-2, GCOV, OPERA and LANDFIRE rasters are staged on `smm` only. GEDI is 1 km and ends
  in 2023-03. Canopy is forest only.
- **Untested paths:**
  - the FIRMS area API (current year) was never run;
  - ERA5-Land ran for one month only (2026-10 on `ca`);
  - biomass was tested with synthetic drops only;
  - Docker was never run.
- **Operations:**
  - one writer per lake root, with no locking;
  - catalog search reads every item JSON (fine for thousands of items);
  - the API has no authentication and no CQL2/sort/fields;
  - Sentinel-2 point sampling is slow.
- **Licences:** `fems-lfmc`, `firms`, `fire-perimeters`, `fire-ignitions` (WFIGS rows) and `biomass` are unknown
  or unclear and treated as non-redistributable. The Scott & Burgan table's public-domain status is assumed.
- **Submission:** the repository is private. Space Apps needs public links, so publish the code (Apache-2.0) and
  only redistributable data.


## Licence

Code: Apache-2.0, see [LICENSE](LICENSE). Data keep their own licences (see [Required
attribution](#required-attribution) and `uv run fuellake licenses --markdown`).
