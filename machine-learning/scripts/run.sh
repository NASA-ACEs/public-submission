#!/usr/bin/env bash
# From a fresh machine to the map: data from the providers, fuel-lake from ../data-aggregation.
# Usage: scripts/run.sh [setup] [data] [train] [map]     (no arguments = all four, in that order)
#        scripts/run.sh nisar                           (optional, after map: NISAR-fed LFMC against 2026 field samples)
# Needs: uv, and ENV_FILE with EARTHDATA_TOKEN=... for the data step.
set -euo pipefail

ML=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORK=${WORK:-$HOME/aces}
DA=${DA:-$ML/../data-aggregation}                  # fuel-lake, next to this project
export LAKE_ROOT=${LAKE_ROOT:-$HOME/fuel-lake-data}
export FUELLAKE_CONFIG_DIR=$DA/config
ENV_FILE=${ENV_FILE:-$HOME/.config/fuel-lake/secrets.env}
MAP_DATE=${MAP_DATE:-2026-10-20}     # newest NISAR SME2 over California was 2026-10-21 (checked 2026-11-14)
MODELS=${MODELS:-core}
STATICS=${STATICS:-}                 # AOI with LANDFIRE staged at 200 m (e.g. ca): cell means replace the table's point values
RAW_TABLE=$WORK/points_2016_2024_lake.parquet       # as built by fuel-lake
TABLE=$WORK/points_2016_2024.parquet                # plus the features of ml.series (daily stores, fire perimeters)
UNUSED="firms smap-l3e smap-climatology gedi-l4b era5-land-daily nisar-sme2 build-points validate"

run() { echo "+ $*"; [[ "${DRY_RUN:-0}" == 1 ]] || "$@"; }
fl() { run uv run --project "$DA" fuellake "$@"; }
ml() { run uv run --project "$ML" python -m "$@"; }

setup() {
  command -v uv >/dev/null || { echo "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 2; }
  mkdir -p "$WORK"
  uv sync -q --project "$DA"
  uv sync -q --project "$ML"
}

data() {
  [[ -r $ENV_FILE ]] || { echo "missing $ENV_FILE (a line EARTHDATA_TOKEN=...)" >&2; exit 2; }
  set -a; . "$ENV_FILE"; set +a
  (cd "$DA" && AOIS=ca CURRENT_START=2026-05-01 SKIP="$UNUSED" ENV_FILE="$ENV_FILE" scripts/full_run.sh) \
    || echo "full_run.sh reported failed steps (see ~/fuel-lake-logs); continuing"
  fl build-points --aoi ca --start 2016-01-01 --end 2024-12-31 --no-sentinel2
  run cp "$LAKE_ROOT/features/ca/point_features.parquet" "$RAW_TABLE"
  ml ml.series --table "$RAW_TABLE" --out "$TABLE" ${STATICS:+--statics "$STATICS"}
  # the NISAR term needs 6 earlier scenes of a track (12-day repeat) before a pass within 12 days of the map date
  fl run nisar-sme2 --aoi ca --start "$(date -u -d "$MAP_DATE -110 days" +%F)" --end "$MAP_DATE" \
    || echo "nisar-sme2 reported failed units; continuing"
}

train() {
  ml ml.data "$TABLE"
  ml ml.train --table "$TABLE" --out "$WORK/models"
}

map() {
  ml ml.grid --check "$TABLE" || echo "grid check failed; continuing"
  ml ml.map --date "$MAP_DATE" --models "$WORK/models/$MODELS" --out "$WORK/outputs/ca-$MAP_DATE"
}

nisar() {
  set -a; . "$ENV_FILE"; set +a
  fl build-points --aoi ca --start "$(date -u -d "$MAP_DATE -30 days" +%F)" --end "$MAP_DATE" --no-sentinel2
  run cp "$LAKE_ROOT/features/ca/point_features.parquet" "$WORK/points_2026.parquet"
  ml ml.nisar_check --table "$WORK/points_2026.parquet" --models "$WORK/models/$MODELS"
}

[[ $# -gt 0 ]] || set -- setup data train map
for step in "$@"; do "$step"; done
