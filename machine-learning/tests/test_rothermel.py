"""Checks of rothermel.py. The pyretechnics comparison is skipped when that package is not installed
(uv run --with pyretechnics --with pytest pytest test_rothermel.py)."""
import numpy as np
import pytest

from ml import rothermel as ro

CODES = sorted(ro.FBFM40)


def scenario(n, seed=0):
    r = np.random.default_rng(seed)
    return dict(m_1h=r.uniform(2, 14, n), m_10h=r.uniform(3, 16, n), m_100h=r.uniform(4, 20, n), m_herb=r.uniform(30, 150, n),
                m_woody=r.uniform(50, 200, n), wind=r.uniform(0, 9, n), wind_from=r.uniform(0, 360, n),
                slope=r.uniform(0, 40, n), aspect=r.uniform(0, 360, n))


def test_table_is_complete_and_matches_printed_packing_ratio():
    assert len(CODES) == 40 and {c // 20 for c in CODES} == {5, 6, 7, 8, 9, 10}
    name, depth, _, _, w1, w10, w100, wh, ww, *_ = ro.FBFM40[101]
    assert name == "GR1" and round((w1 + w10 + w100 + wh + ww) * ro.T_AC / depth / ro.RHO_P, 5) == 0.00143   # RMRS-GTR-153 p. 26


def compare(n):
    import pyretechnics.fuel_models as fm
    import pyretechnics.surface_fire as sf
    import pyretechnics.vector_utils as vu
    err = {k: [] for k in ("ros", "intensity", "flame_length", "ros_no_wind", "lw_ratio", "direction_deg")}
    for code in CODES:
        s = scenario(n, code)
        got = ro.spread(code, **s)
        for i in range(n):
            m = [s[k][i] / 100 for k in ("m_1h", "m_10h", "m_100h", "m_1h", "m_herb", "m_woody")]
            lo = sf.calc_surface_fire_behavior_no_wind_no_slope(fm.moisturize(fm.get_fuel_model(code), m))
            hi = sf.calc_surface_fire_behavior_max(lo, s["wind"][i] * 60, s["wind_from"][i], np.tan(np.deg2rad(s["slope"][i])), s["aspect"][i])
            ref = dict(ros=hi["max_spread_rate"], intensity=hi["max_fireline_intensity"], flame_length=hi["max_flame_length"],
                       ros_no_wind=lo["base_spread_rate"], lw_ratio=hi["length_to_width_ratio"])
            for k, v in ref.items():
                if v > 1e-3:
                    err[k].append(abs(got[k][i] / v - 1))
                else:
                    assert got[k][i] < 2e-3
            if hi["max_spread_rate"] > 1e-3:
                d = abs(got["direction"][i] - vu.spread_direction_vector_to_angle(hi["max_spread_direction"]))
                err["direction_deg"].append(min(d, 360 - d))
    return {k: np.array(v) for k, v in err.items()}


def test_equations_match_pyretechnics_with_its_parameters(monkeypatch):
    fm = pytest.importorskip("pyretechnics.fuel_models")
    p = ro._P.copy()
    for c in CODES:                                    # pyretechnics stores loads in lb/ft2 rounded to 4 decimals
        m = fm.get_fuel_model(c)
        p[c] = [m["delta"], m["M_x"][0] * 100, m["h"][0], *(m["w_o"][j] / ro.T_AC for j in (0, 1, 2, 4, 5)), m["sigma"][0], m["sigma"][4], m["sigma"][5]]
    monkeypatch.setattr(ro, "_P", p)
    err = compare(100)
    print({k: float(v.max()) for k, v in err.items()})
    assert all(v.max() < 2e-3 for k, v in err.items() if k != "direction_deg") and err["direction_deg"].max() < 0.05


def test_published_parameters_stay_close_to_pyretechnics():
    pytest.importorskip("pyretechnics.fuel_models")
    err = compare(100)                                 # exact Table 7 loads here, rounded loads there
    print({k: (float(np.median(v)), float(np.quantile(v, 0.99)), float(v.max())) for k, v in err.items()})
    assert np.quantile(err["ros"], 0.99) < 0.02 and err["ros"].max() < 0.10 and np.median(err["ros"]) < 0.005


def test_physical_behaviour():
    base = dict(m_1h=6, m_10h=7, m_100h=8, m_herb=60, m_woody=90, wind=3, wind_from=270, slope=0, aspect=0)
    for code in CODES:
        r = ro.spread(code, **base)
        assert r["ros"] > 0 and r["flame_length"] > 0 and abs(r["direction"] - 90) < 1e-6          # wind from west -> east
        assert ro.spread(code, **{**base, "wind": 6})["ros"] >= r["ros"]                              # equal at the wind limit
        assert ro.spread(code, **{**base, "wind": 1})["ros"] < r["ros"]
        assert ro.spread(code, **{**base, "m_1h": 10, "m_10h": 11, "m_100h": 12})["ros"] < r["ros"]
        assert ro.spread(code, **{**base, "m_1h": 60, "m_10h": 60, "m_100h": 60, "m_herb": 30})["ros"] == 0   # above extinction
    up = ro.spread(145, **{**base, "wind": 0, "slope": 30, "aspect": 180})
    assert abs(up["direction"] - 0) < 1e-6 and up["ros"] > ro.spread(145, **{**base, "wind": 0})["ros"]   # south-facing: upslope = north
    for code in (142, 145, 147, 165):                                                               # live woody fuel: drier LFMC, faster fire
        assert ro.spread(code, **{**base, "m_woody": 60})["ros"] > ro.spread(code, **{**base, "m_woody": 140})["ros"]
    for code in (101, 102, 121, 122):                                                               # dynamic: cured grass, faster fire
        assert ro.spread(code, **{**base, "m_herb": 30})["ros"] > ro.spread(code, **{**base, "m_herb": 120})["ros"]


def test_non_burnable_unknown_and_shapes():
    r = ro.spread(np.array([[91, 99], [np.nan, 122]]), 6, 7, 8, 60, 90, 3)
    assert r["ros"].shape == (2, 2) and r["ros"][0, 0] == 0 and r["ros"][0, 1] == 0 and np.isnan(r["ros"][1, 0]) and r["ros"][1, 1] > 0


def test_inputs_and_grid():
    assert abs(float(ro.emc(20, 25)) - (2.22749 + 0.160107 * 20 - 0.014784 * 77)) < 1e-9
    m1, m10 = ro.dead_moisture(15, 308.15)
    assert 2 < m1 < 5 and abs(m10 / m1 - 1.28 / 1.03) < 1e-9
    assert abs(float(ro.midflame_wind(1.15, 122)) - 1.83 / np.log((20 + 0.36 * 1.5) / (0.13 * 1.5))) < 1e-9   # GS2, 1.5 ft deep
    assert float(ro.midflame_wind(5, 165, 60, 20)) < float(ro.midflame_wind(5, 165))                # sheltered under canopy
    n = (50, 40)
    r = np.random.default_rng(0)
    g = ro.grid_layers(r.choice(CODES + [91], n), r.uniform(0, 10, n), r.uniform(0, 360, n), r.uniform(5, 60, n), r.uniform(285, 315, n),
                       r.uniform(4, 20, n), r.uniform(50, 180, n), r.uniform(0, 35, n), r.uniform(0, 360, n), chunk=700)
    assert set(g) == {"rate_of_spread", "flame_length", "fireline_intensity", "spread_direction"}
    assert all(v.shape == n and v.dtype == np.float32 and np.isfinite(v).all() for v in g.values())


def test_midflame_wind_matches_pyretechnics():
    sf = pytest.importorskip("pyretechnics.surface_fire")
    for code, cover, height in ((122, 0, 0), (165, 60, 20), (145, 10, 3), (183, 85, 35), (102, 40, 1)):
        ref = sf.calc_midflame_wind_speed(5 / 1.15, ro.FBFM40[code][1], height / ro.FT, cover / 100)
        assert abs(float(ro.midflame_wind(5, code, cover, height)) / ref - 1) < 1e-3
