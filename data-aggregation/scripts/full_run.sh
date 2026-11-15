#!/usr/bin/env bash
# fuel-lake — full California download and gold build on the team server (idempotent: re-run to resume)
# Licensed under Apache-2.0.
#
# Usage (from anywhere):
#   ENV_FILE=~/.config/fuel-lake/secrets.env LAKE_ROOT=/data/fuel-lake scripts/full_run.sh
#   DRY_RUN=1 START=2026-10-01 END=2026-10-31 scripts/full_run.sh      # print the commands only
#
# Parameters (environment variables; all optional):
#   LAKE_ROOT          lake root, local path or s3://bucket/prefix (default: the fuellake default, ../fuel-lake-data)
#   ENV_FILE           file with KEY=VALUE credentials to load first (keep it outside the repo; never printed)
#   START, END         whole period (default 2015-10-01 .. today)
#   TRAIN_END          end of the training period (default 2024-12-31); the run goes START..TRAIN_END first,
#                      then CURRENT_START..END (CURRENT_START defaults to 2025-01-01)
#   AOIS               AOIs to run (default "ca smm"); region-scale steps run on the region AOIs, local steps on
#                      the local AOIs (scale read from config/aois/<id>.yaml)
#   SKIP               datasets or steps to skip, space or comma separated: a dataset id ("hrrr-gusts"), a dataset
#                      on one AOI ("gridmet@smm") or a gold step ("build-points", "build-features", "validate")
#   POINTS_START, POINTS_END   build-points period (default 2016-01-01 .. 2024-12-31, the comparable FPA-FOD years)
#   S2_MAX_POINTS      Sentinel-2 point-dates in build-points (default 5000; "all" = no cap, 0 = none)
#   CUBE_START         start of the smm gold cube (default 2026-06-01; matchups always use START..END)
#   HRRR_START         first day of HRRR gusts on smm (default 2025-01-01; about 140 MB download per month)
#   SME2_ORBITS        "both" (default) or "descending": NISAR SME2 pass directions on the region AOI
#   ERA5_HISTORY=1     also fetch ERA5-Land for the training period (CDS runs one job per user: many hours);
#                      default: ERA5-Land for the current period only, after everything else in its AOI
#   DRY_RUN=1          print the commands, run nothing (not even init)
#   LOG_DIR            log directory (default ~/fuel-lake-logs)
#
# Credentials are read from the environment by name only; a dataset whose credential is missing is skipped and
# listed at the end. Each step logs and continues on failure; the exit code is 1 if any step failed. Steps run one
# after the other: the lake supports a single writer, and gridMET must never run twice at the same time.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO"

if [[ -n "${ENV_FILE:-}" ]]; then
  if [[ ! -r "$ENV_FILE" ]]; then echo "error: ENV_FILE $ENV_FILE is not readable" >&2; exit 2; fi
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

TODAY=$(date +%F)
START=${START:-2015-10-01}
END=${END:-$TODAY}
TRAIN_END=${TRAIN_END:-2024-12-31}
CURRENT_START=${CURRENT_START:-2025-01-01}
AOIS=${AOIS:-ca smm}
SKIP=${SKIP:-}
POINTS_START=${POINTS_START:-2016-01-01}
POINTS_END=${POINTS_END:-2024-12-31}
S2_MAX_POINTS=${S2_MAX_POINTS:-5000}
CUBE_START=${CUBE_START:-2026-06-01}
HRRR_START=${HRRR_START:-2025-01-01}
ERA5_HISTORY=${ERA5_HISTORY:-0}
SME2_ORBITS=${SME2_ORBITS:-both}
DRY_RUN=${DRY_RUN:-0}
LOG_DIR=${LOG_DIR:-$HOME/fuel-lake-logs}

for d in START END TRAIN_END CURRENT_START POINTS_START POINTS_END CUBE_START HRRR_START; do
  if [[ ! "${!d}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then echo "error: $d must be YYYY-MM-DD, got ${!d}" >&2; exit 2; fi
done
if [[ "$END" < "$START" ]]; then echo "error: END $END is before START $START" >&2; exit 2; fi
case "$SME2_ORBITS" in both|descending) ;; *) echo "error: SME2_ORBITS must be both or descending" >&2; exit 2 ;; esac

if command -v uv >/dev/null 2>&1; then FL=(uv run --project "$REPO" fuellake); else FL=(fuellake); fi

RUN_TAG=$(date -u +%Y%m%dT%H%M%SZ)
if [[ "$DRY_RUN" == 1 ]]; then
  LOG=/dev/null
else
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/full_run_$RUN_TAG.log"
fi
FAILED=()
SKIPPED=()

log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
min_date() { if [[ "$1" < "$2" ]]; then echo "$1"; else echo "$2"; fi; }
max_date() { if [[ "$1" > "$2" ]]; then echo "$1"; else echo "$2"; fi; }
aoi_scale() { sed -n 's/^scale:[[:space:]]*\([a-z]*\).*/\1/p' "$REPO/config/aois/$1.yaml" 2>/dev/null | head -1; }

is_skipped() {  # is_skipped <name> [<aoi>]
  local s
  for s in ${SKIP//,/ }; do
    if [[ "$s" == "$1" || ( -n "${2:-}" && "$s" == "$1@$2" ) ]]; then return 0; fi
  done
  return 1
}

missing_env() {  # names of the unset variables among the arguments
  local v out=()
  for v in "$@"; do [[ -n "${!v:-}" ]] || out+=("$v"); done
  echo "${out[*]:-}"
}

exec_step() {  # exec_step <label> <command...>: log, run, record a failure and go on
  local label=$1; shift
  log "RUN  $label: $*"
  if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
  local t0=$SECONDS rc=0
  "$@" 2>&1 | tee -a "$LOG" || rc=$?
  if [[ $rc -eq 0 ]]; then
    log "OK   $label ($((SECONDS - t0)) s)"
  else
    FAILED+=("$label")
    log "FAIL $label (exit $rc, $((SECONDS - t0)) s)"
  fi
}

ingest() {  # ingest <dataset> <aoi> <start> <end> [env vars needed...] [-- extra fuellake run args]
  local ds=$1 aoi=$2 s=$3 e=$4; shift 4
  local needs=() extra=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do needs+=("$1"); shift; done
  if [[ "${1:-}" == "--" ]]; then shift; extra=("$@"); fi
  local label="$ds@$aoi $s..$e"
  if is_skipped "$ds" "$aoi"; then log "SKIP $label (SKIP)"; SKIPPED+=("$label: SKIP"); return 0; fi
  if [[ "$e" < "$s" ]]; then return 0; fi
  local miss
  miss=$(missing_env "${needs[@]+"${needs[@]}"}")
  if [[ -n "$miss" ]]; then log "SKIP $label (missing $miss)"; SKIPPED+=("$label: missing $miss"); return 0; fi
  exec_step "$label" "${FL[@]}" run "$ds" --aoi "$aoi" --start "$s" --end "$e" "${extra[@]+"${extra[@]}"}"
}

era5() {  # era5 <aoi> <start> <end>: the training history only with ERA5_HISTORY=1 (CDS: one job per user)
  if [[ "$ERA5_HISTORY" != 1 && "$3" < "$C_START" ]]; then
    log "SKIP era5-land-daily@$1 $2..$3 (history: set ERA5_HISTORY=1)"; SKIPPED+=("era5-land-daily@$1 $2..$3: ERA5_HISTORY=0")
    return 0
  fi
  ingest era5-land-daily "$1" "$2" "$3" CDSAPI_URL CDSAPI_KEY
}

gold() {  # gold <step> <command args...>
  local name=$1; shift
  if is_skipped "$name"; then log "SKIP $name (SKIP)"; SKIPPED+=("$name: SKIP"); return 0; fi
  exec_step "$name $*" "${FL[@]}" "$@"
}

# ---------------------------------------------------------------------------- set-up
REGION_AOIS=() LOCAL_AOIS=()
for a in $AOIS; do
  case "$(aoi_scale "$a")" in
    region) REGION_AOIS+=("$a") ;;
    local) LOCAL_AOIS+=("$a") ;;
    *) echo "error: unknown AOI $a (no config/aois/$a.yaml with a scale)" >&2; exit 2 ;;
  esac
done

T_START=$START; T_END=$(min_date "$TRAIN_END" "$END")
C_START=$(max_date "$CURRENT_START" "$START"); C_END=$END
PERIODS=()
[[ "$T_END" < "$T_START" ]] || PERIODS+=("$T_START $T_END")
[[ "$C_END" < "$C_START" ]] || PERIODS+=("$C_START $C_END")
END_YEAR=${END:0:4}
FIRMS_ARCHIVE_END="$((END_YEAR - 1))-12-31"   # the keyless yearly archive ends with the previous year

log "fuel-lake full run $RUN_TAG (dry run: $DRY_RUN), repo $REPO"
log "lake root: ${LAKE_ROOT:-<fuellake default>}; log: $LOG"
log "periods: ${PERIODS[*]:-none}; region AOIs: ${REGION_AOIS[*]:-none}; local AOIs: ${LOCAL_AOIS[*]:-none}"
for v in EARTHDATA_TOKEN FIRMS_MAP_KEY CDSAPI_URL CDSAPI_KEY; do
  if [[ -n "${!v:-}" ]]; then log "credential $v: set"; else log "credential $v: missing"; fi
done
if [[ "$START" > "2015-10-01" || "$T_END" < "2024-12-31" ]]; then
  log "note: smap-climatology needs SMAP L4 and L3E for all of 2015-10-01..2024-12-31; it discovers nothing until then"
fi

gold init init

# SME2 on the region AOI with both pass directions: a config overlay with options.region_orbit_state: null
SME2_CONFIG_ENV=()
if [[ "$SME2_ORBITS" == both ]]; then
  OVERLAY="${LOG_DIR}/config-overlay-sme2-both"
  log "SME2 overlay: $OVERLAY (config/ copy with nisar-sme2 options.region_orbit_state: null)"
  if [[ "$DRY_RUN" != 1 ]]; then
    rm -rf "$OVERLAY" && mkdir -p "$OVERLAY" && cp -R "$REPO/config/." "$OVERLAY/"
    sed -i.bak 's/^\([[:space:]]*region_orbit_state:\).*/\1 null   # full_run.sh overlay: both pass directions/' \
      "$OVERLAY/datasets/nisar-sme2.yaml" && rm -f "$OVERLAY/datasets/nisar-sme2.yaml.bak"
    grep -q 'region_orbit_state: null' "$OVERLAY/datasets/nisar-sme2.yaml" \
      || { log "FAIL SME2 overlay: region_orbit_state not found"; FAILED+=("sme2-overlay"); }
  fi
  SME2_CONFIG_ENV=(env "FUELLAKE_CONFIG_DIR=$OVERLAY")
fi

# ---------------------------------------------------------------------------- 1. region scale (ca): training inputs
for aoi in "${REGION_AOIS[@]+"${REGION_AOIS[@]}"}"; do
  # labels
  ingest globe-lfmc "$aoi" 1977-01-01 2023-12-31
  for p in "${PERIODS[@]+"${PERIODS[@]}"}"; do
    set -- $p
    ingest fems-lfmc "$aoi" "$1" "$2"
    ingest fire-ignitions "$aoi" "$1" "$2"
    ingest fire-perimeters "$aoi" "$1" "$2"
    if [[ -n "${FIRMS_MAP_KEY:-}" ]]; then
      ingest firms "$aoi" "$1" "$2" FIRMS_MAP_KEY
    else
      ingest firms "$aoi" "$1" "$(min_date "$2" "$FIRMS_ARCHIVE_END")"
      if [[ "$2" > "$FIRMS_ARCHIVE_END" ]]; then
        log "SKIP firms@$aoi $END_YEAR (missing FIRMS_MAP_KEY; the keyless archive ends $FIRMS_ARCHIVE_END)"
        SKIPPED+=("firms@$aoi $END_YEAR: missing FIRMS_MAP_KEY")
      fi
    fi
  done
  # weather and soil moisture (form B, native grids); gridMET strictly sequential
  for p in "${PERIODS[@]+"${PERIODS[@]}"}"; do
    set -- $p
    ingest gridmet "$aoi" "$1" "$2"
    ingest smap-l4 "$aoi" "$1" "$2" EARTHDATA_TOKEN
    ingest smap-l3e "$aoi" "$1" "$2" EARTHDATA_TOKEN
  done
  ingest smap-climatology "$aoi" 2015-10-01 2024-12-31 EARTHDATA_TOKEN   # one unit; needs both full SMAP baselines
  ingest gedi-l4b "$aoi" "$START" "$END" EARTHDATA_TOKEN
  # vegetation class shares: 60 blocks of 30 m clips per release (about 210 MB), reused on a rerun
  ingest landfire-evt "$aoi" "$START" "$END"
done

# ---------------------------------------------------------------------------- 2. statewide training table
S2_ARGS=()
case "$S2_MAX_POINTS" in
  all) ;;
  *[!0-9]*|'') echo "error: S2_MAX_POINTS must be a number or all" >&2; exit 2 ;;
  *) S2_ARGS=(--s2-max-points "$S2_MAX_POINTS") ;;
esac
for aoi in "${REGION_AOIS[@]+"${REGION_AOIS[@]}"}"; do
  # one table for both systems (ignitions + LFMC); LANDFIRE is read remotely per point, so no smm step is needed
  gold build-points build-points --aoi "$aoi" --start "$POINTS_START" --end "$POINTS_END" "${S2_ARGS[@]+"${S2_ARGS[@]}"}"
done

# ---------------------------------------------------------------------------- 3. NISAR SME2 statewide (2025 on)
for aoi in "${REGION_AOIS[@]+"${REGION_AOIS[@]}"}"; do
  if ! is_skipped nisar-sme2 "$aoi" && [[ -z "$(missing_env EARTHDATA_TOKEN)" ]] && [[ ! "$C_END" < "$C_START" ]]; then
    exec_step "nisar-sme2@$aoi $C_START..$C_END ($SME2_ORBITS)" "${SME2_CONFIG_ENV[@]+"${SME2_CONFIG_ENV[@]}"}" \
      "${FL[@]}" run nisar-sme2 --aoi "$aoi" --start "$C_START" --end "$C_END"
  else
    ingest nisar-sme2 "$aoi" "$C_START" "$C_END" EARTHDATA_TOKEN   # logs the skip reason
  fi
done

# ---------------------------------------------------------------------------- 4. local scale (smm)
for aoi in "${LOCAL_AOIS[@]+"${LOCAL_AOIS[@]}"}"; do
  ingest landfire-topo "$aoi" "$START" "$END"
  ingest cop-dem "$aoi" "$START" "$END"
  ingest landfire-fbfm40 "$aoi" "$START" "$END"         # every configured release (LF 2016, 2022, 2025)
  ingest landfire-fuel-load "$aoi" "$START" "$END"      # derived from the fbfm40 items: after them
  ingest landfire-canopy "$aoi" "$START" "$END"
  ingest landfire-evt "$aoi" "$START" "$END"            # class shares, every configured release
  ingest gedi-l4b "$aoi" "$START" "$END" EARTHDATA_TOKEN
  for p in "${PERIODS[@]+"${PERIODS[@]}"}"; do           # the smm cube and matchups need their own form B items
    set -- $p
    ingest gridmet "$aoi" "$1" "$2"
    ingest smap-l4 "$aoi" "$1" "$2" EARTHDATA_TOKEN
    ingest smap-l3e "$aoi" "$1" "$2" EARTHDATA_TOKEN
    ingest sentinel2-indices "$aoi" "$1" "$2"
    ingest hrrr-gusts "$aoi" "$(max_date "$1" "$HRRR_START")" "$2"   # about 140 MB of GRIB messages per month
  done
  # NISAR and OPERA radar exist for the current period; the first SME2 run re-stages the older smm items once
  # (--force) so that they carry the current fuellake:status
  marker="$LOG_DIR/.sme2-$aoi-forced"
  if [[ -f "$marker" ]]; then
    ingest nisar-sme2 "$aoi" "$C_START" "$C_END" EARTHDATA_TOKEN
  else
    n_fail=${#FAILED[@]}
    ingest nisar-sme2 "$aoi" "$C_START" "$C_END" EARTHDATA_TOKEN -- --force
    if [[ "$DRY_RUN" != 1 && ${#FAILED[@]} -eq $n_fail && -n "${EARTHDATA_TOKEN:-}" ]] && ! is_skipped nisar-sme2 "$aoi"; then
      touch "$marker"
    fi
  fi
  ingest nisar-gcov "$aoi" "$C_START" "$C_END" EARTHDATA_TOKEN
  ingest opera-rtc-s1 "$aoi" "$C_START" "$C_END" EARTHDATA_TOKEN
done

# ---------------------------------------------------------------------------- 5. ERA5-Land (CDS: one job per user)
for aoi in "${REGION_AOIS[@]+"${REGION_AOIS[@]}"}" "${LOCAL_AOIS[@]+"${LOCAL_AOIS[@]}"}"; do
  [[ -n "$aoi" ]] || continue
  for p in "${PERIODS[@]+"${PERIODS[@]}"}"; do
    set -- $p
    era5 "$aoi" "$1" "$2"
  done
done

# ---------------------------------------------------------------------------- 6. smm gold layer and validation
for aoi in "${LOCAL_AOIS[@]+"${LOCAL_AOIS[@]}"}"; do
  gold build-features build-features --aoi "$aoi" --what matchups --start "$START" --end "$END"
  cube_start=$(max_date "$CUBE_START" "$START")
  if [[ "$cube_start" > "$END" ]]; then cube_start=$START; fi
  gold build-features build-features --aoi "$aoi" --what cube --start "$cube_start" --end "$END"
done
gold validate validate

# ---------------------------------------------------------------------------- summary
log "skipped: ${#SKIPPED[@]}"
for s in "${SKIPPED[@]+"${SKIPPED[@]}"}"; do log "  $s"; done
if [[ ${#FAILED[@]} -gt 0 ]]; then
  log "FAILED steps: ${#FAILED[@]} (re-run the script to retry; cataloged units are skipped)"
  for s in "${FAILED[@]}"; do log "  $s"; done
  exit 1
fi
log "done: no failed step"
