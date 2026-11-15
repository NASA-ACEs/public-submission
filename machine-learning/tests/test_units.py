import numpy as np

from ml import data, fuelage, series


def test_series_windows_look_back_only():
    x = np.arange(400, dtype="float32").reshape(400, 1, 1)
    assert series.derive(x, "sum", 30)[100, 0, 0] == sum(range(70, 100))          # the 30 days before day 100
    assert series.derive(x, "mean", 30)[100, 0, 0] == np.mean(range(70, 100))
    assert series.derive(x, "max", 3)[100, 0, 0] == 100                           # the day and the two before it
    z = series.derive(x, "z", 365)
    assert np.isnan(z[100, 0, 0]) and z[399, 0, 0] > 0                            # needs most of a year of history


def test_fuel_age():
    years = np.array([0.5, 1, 2, 3.4, 3.6, 6, 9, 10])
    m = fuelage.odds(years, 12.0)
    assert np.all(np.diff(m[:4]) > 0) and m[0] < 0.01 and np.all(m[4:] == 1)      # young scars only
    forest = fuelage.odds(years, 12.0, np.full(len(years), fuelage.FOREST))
    assert np.all(forest <= m) and forest[5] < 1 and forest[6] == 1               # former forest: 7 years
    p = np.full(len(years), 0.01)
    assert np.all(fuelage.apply(p, years, 12.0) <= p) and fuelage.apply(p, years, 0.0)[0] == 0.01
    perm = [fuelage.permeability([2.0], np.array([k]))[0] for k in (fuelage.FOREST, fuelage.SHRUB, fuelage.GRASS)]
    assert perm[0] < perm[1] < perm[2] <= 1 and fuelage.permeability([10.0])[0] == 1
    assert fuelage.vegetation([91, 102, 122, 145, 165, 185, 203, np.nan]).tolist() == [0, 3, 2, 2, 1, 1, 1, 0]


def test_fuel_age_fit_recovers_beta():
    rng = np.random.default_rng(0)
    n = 200_000
    years = np.where(rng.random(n) < 0.3, rng.uniform(0.1, 3.4, n), fuelage.CAP)
    p = np.full(n, 0.05)
    y = rng.random(n) < fuelage.apply(p, years, 2.0)
    assert abs(fuelage.fit(p, years, y) - 2.0) < 0.4


def test_disc_mean_equals_a_loop():
    from ml import context
    rng = np.random.default_rng(0)
    a = rng.normal(size=(40, 55))
    a[rng.random(a.shape) < 0.2] = np.nan
    got, r = context.disc_mean(a, 5), 5
    for i, j in [(0, 0), (3, 50), (20, 20), (39, 54), (17, 2)]:
        y, x = np.mgrid[max(i - r, 0):min(i + r + 1, 40), max(j - r, 0):min(j + r + 1, 55)]
        near = a[y, x][(y - i) ** 2 + (x - j) ** 2 <= r * r]
        want = np.nanmean(near) if np.isfinite(a[i, j]) else np.nan
        assert (np.isnan(got[i, j]) and np.isnan(want)) or abs(got[i, j] - want) < 1e-4


def test_feature_lists():
    for sets in data.FEATURES.values():
        assert "lfmc_hat" not in sets["fire"] and not set(data.WIND) & set(sets["lfmc"])
        assert set(data.SERIES_ALL) <= set(sets["fire"]) and len(set(sets["fire"])) == len(sets["fire"])
        assert not set(data.EVT) & set(sets["lfmc"])
    assert set(data.EVT) <= set(data.FEATURES["full"]["fire"]) and not set(data.EVT) & set(data.FEATURES["core"]["fire"])
    assert set(data.CONTEXT) <= set(data.FEATURES["full"]["fire"]) and not set(data.CONTEXT) & set(data.FEATURES["full"]["lfmc"])
    assert set(data.CONTEXT_DATED) <= set(data.DATED) and not {"tpi_r1km", "elevation_sd_r1km"} & set(data.DATED)
    assert all(data.split(c)[0] in data.SERIES for c in data.SERIES_ALL)
    assert all(v in (1, -1) for v in data.MONOTONE.values())
