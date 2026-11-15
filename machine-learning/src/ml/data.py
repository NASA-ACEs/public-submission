import zlib

import numpy as np
import pandas as pd

GLOBE_LAST_YEAR = 2023
FIRE_YEARS = (2016, 2024)
LAGS = (15, 30, 45, 60, 90)
GRIDMET = ["vs", "rmin", "erc", "bi", "fm100", "fm1000", "vpd", "pr", "tmmx", "pet"]
GRIDMET_LAGGED = ["fm100", "fm1000", "vpd", "pr", "tmmx", "pet"]
SMAP = ["sm_surface", "sm_rootzone"]
STATIC = ["total_load", "dead_1h_load", "dead_10h_load", "dead_100h_load", "live_herb_load", "live_woody_load",
          "fuel_bed_depth", "moisture_of_extinction", "burnable", "canopy_cover", "canopy_height",
          "canopy_base_height", "canopy_bulk_density", "elevation", "slope", "northness", "eastness"]
# share of the 200 m cell under each LANDFIRE existing-vegetation class (docs/research/vegetation-type.md): large-fire
# model only. The share that is built up carries most of it.
EVT = [f"evt_share_{c}" for c in ("chaparral", "coastal_scrub", "sagebrush", "desert_scrub", "other_shrub", "oak_woodland",
                                  "other_hardwood", "conifer_oak", "dry_conifer", "mesic_conifer", "juniper_pinyon", "grass",
                                  "riparian", "sparse", "agriculture", "developed")]
# static inputs are means over 200 m cells, in the table and on the map (docs/research/ca-statics.md): a 30 m pixel
# at an ignition is a road as often as not. A LANDFIRE release is used from the day after its disturbance window.
CELL_M = 200
LANDFIRE_FROM = {2016: "2017-01-01", 2022: "2022-10-01", 2025: "2025-10-01"}
# the surroundings of the cell (ml.context): means over discs of 1 and 3 km on the 200 m California grid, terrain
# roughness and position. Large-fire model only (docs/research/landscape-context.md)
CONTEXT_DATED = [f"{v}_{r}" for v in ("evt_share_developed", "evt_share_agriculture", "burnable", "total_load", "canopy_cover",
                                      "forest_share", "shrub_share") for r in ("r1km", "r3km")]
CONTEXT = CONTEXT_DATED + ["elevation_sd_r1km", "tpi_r1km", "tpi_r3km"]
DATED = [c for c in STATIC + EVT if c not in ("elevation", "slope", "northness", "eastness")] + CONTEXT_DATED     # missing before 2017
STATIC_ASSETS = {"landfire-topo": ["elevation", "slope", "aspect"],
                 "landfire-fuel-load": ["total_load", "dead_1h_load", "dead_10h_load", "dead_100h_load", "live_herb_load",
                                        "live_woody_load", "fuel_bed_depth", "moisture_of_extinction", "burnable", "burnable_share"],
                 "landfire-canopy": ["canopy_cover", "canopy_height", "canopy_base_height", "canopy_bulk_density"],
                 "landfire-evt": EVT}


def lagged(names):
    return [f"{n}_lag{lag}d" for n in names for lag in LAGS]


def split(col):
    name, _, lag = col.partition("_lag")
    return name, int(lag[:-1]) if lag else 0


WEATHER = GRIDMET + lagged(GRIDMET_LAGGED)
SOIL = SMAP + lagged(SMAP)
CORE = WEATHER + SOIL + ["doy_sin", "doy_cos"]
# computed from the lake's daily stores by ml.series, for table rows and map cells alike:
# name: (dataset, variable, operation, days). "z" = value of the day against the mean and spread of the days before
SERIES = {"rz_z12": ("smap-l4", "sm_rootzone", "z", 365), "sf_z12": ("smap-l4", "sm_surface", "z", 365),
          "pr_sum30": ("gridmet", "pr", "sum", 30), "pr_sum90": ("gridmet", "pr", "sum", 90),
          "vpd_mean30": ("gridmet", "vpd", "mean", 30), "vs_max3": ("gridmet", "vs", "max", 3),
          "gust_max": ("hrrr-gusts", "gust_max", "max", 1), "wind10_max": ("hrrr-gusts", "wind10_max", "max", 1)}
DRYNESS = ["rz_z12", "sf_z12"] + [f"rz_z12_lag{lag}d" for lag in (30, 60, 90)]
RAIN = ["pr_sum30", "pr_sum90"]
ANTECEDENT = RAIN + ["vpd_mean30", "vs_max3"]
WIND = ["gust_max", "wind10_max"]       # HRRR 3 km, the larger of the 12 and 00 UTC analyses (docs/research/wind.md)
SERIES_ALL = DRYNESS + ANTECEDENT + WIND
# LFMC: weather, soil moisture and their single-day lags, season, rain sums (docs/research/lfmc-s2.md).
# Large fire: weather of the day, HRRR gusts, dryness against the last 12 months, antecedent rain and VPD; no single-day lags,
# raw soil moisture, season or lfmc_hat, which did not hold up in years never seen (docs/research/fire-model.md).
FEATURES = {"core": {"lfmc": CORE + RAIN, "fire": GRIDMET + SERIES_ALL},
            "full": {"lfmc": CORE + STATIC + RAIN, "fire": GRIDMET + STATIC + EVT + SERIES_ALL + CONTEXT}}
# large-fire model only: the sign a feature may act with (LightGBM monotone constraints). It fixes the response
# outside the training range, e.g. where the NISAR term moves a dryness anomaly on the map.
MONOTONE = {**{c: 1 for c in ["vs", "vpd", "tmmx", "erc", "bi", "pet", "slope", "vs_max3", "vpd_mean30"] + WIND},
            **{c: -1 for c in ["rmin", "fm100", "fm1000"] + RAIN + DRYNESS}}


def load_points(path=None):
    if path:
        return pd.read_parquet(path)
    from fuellake.client import LakeClient
    return LakeClient.from_env().read_table("point_features")


def lfmc_rows(pts):
    df = pts[pts["point_type"] == "lfmc_sample"]
    year = pd.to_datetime(df["date"]).dt.year
    src = ((df["source"] == "globe-lfmc") & (year <= GLOBE_LAST_YEAR)) | ((df["source"] == "fems") & (year > GLOBE_LAST_YEAR))
    qc = df["qc_flags"].fillna("").astype(str) if "qc_flags" in df else pd.Series("", index=df.index)
    df = df[src & df["lfmc_pct"].notna() & ~df["outlier_flag"].fillna(False).astype(bool) & ~qc.str.contains("value_out_of_range")]
    g = df.groupby([df["site_id"].fillna(df["point_id"]).rename("site"), "date"], sort=False)
    out = g.first()
    out["lfmc_pct"] = g["lfmc_pct"].mean()
    return out.reset_index()


def ignition_rows(pts):
    df = pts[pts["point_type"] == "fire_ignition"]
    ok = (df["source"] == "fpa-fod") & df["fire_year"].between(*FIRE_YEARS) & df["large_fire"].notna()
    return df[ok].reset_index(drop=True)


def add_derived(df, day=None):
    doy = pd.Timestamp(day).day_of_year if day is not None else pd.to_datetime(df["date"]).dt.day_of_year
    df["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    df["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    if "aspect" in df:
        a = df["aspect"].astype("float64")
        rad = np.deg2rad(a.where(a >= 0))
        df["northness"], df["eastness"] = np.cos(rad), np.sin(rad)
    return df


def X(df, cols):
    return df[cols].astype("float32")


def block_fold(blocks, k=5, seed=0):
    return np.array([zlib.crc32(f"{seed}:{b}".encode()) % k for b in blocks])


if __name__ == "__main__":
    import sys
    pts = load_points(sys.argv[1] if len(sys.argv) > 1 else None)
    for name, rows in (("lfmc", lfmc_rows(pts)), ("ignitions", ignition_rows(pts))):
        rows = add_derived(rows)
        print(f"\n{name}: {len(rows)} rows, {rows['block_50km'].nunique()} blocks")
        if name == "ignitions":
            print(f"large fires: {int(rows['large_fire'].sum())}")
        print(X(rows, FEATURES["full"]).notna().mean().round(3).to_string())
