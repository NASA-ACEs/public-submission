import os
# 2025-2026 WFIGS ignitions with the features of model v2, by the code the map uses (ml.series). Read-only on the lake.
import numpy as np, pandas as pd
from fuellake.client import LakeClient
from ml import data, series
c = LakeClient.from_env()
t = c.read_table("fire_ignitions")
t = t[(t["source"] == "nifc-wfigs") & t["fire_size_ha"].notna()].copy()
t["date"] = pd.to_datetime(t["discovery_date"])
t = t[(t["date"] >= "2025-01-01") & (t["date"] <= "2026-11-10") & t["lon"].between(-124.5, -114) & t["lat"].between(32.4, 42.1)]
pts = pd.DataFrame({"point_type": "fire_ignition", "point_id": t["ignition_id"].astype(str), "source": t["source"], "date": t["date"].dt.date,
                    "lon": t["lon"].astype(float), "lat": t["lat"].astype(float), "fire_year": t["date"].dt.year,
                    "fire_size_ha": t["fire_size_ha"].astype(float), "fire_name": t["fire_name"], "large_fire": t["fire_size_ha"] >= 400}).reset_index(drop=True)
data.SERIES.update({v: ("gridmet", v, "max", 1) for v in data.GRIDMET})     # the value of the day
out = series.add_to_table(c, pts, cols=data.GRIDMET + data.SERIES_ALL, statics="ca")
out.to_parquet(os.path.expanduser("~/spaceapps/ml-work/research/oot/ignitions_2025_2026.parquet"))
print(out.shape, out.groupby("fire_year")["large_fire"].agg(["size", "sum"]).to_dict())
print(out[data.GRIDMET + data.SERIES_ALL + ["elevation", "total_load", "years_since_fire"]].notna().mean().round(3).to_string())
