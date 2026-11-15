"""Checks of spreadsim.py (pytest test_spreadsim.py, with rothermel.py beside it)."""
import numpy as np

from ml import rothermel as ro
from ml import spreadsim as ss


def uniform(n=401, ros=2.0, direction=0.0, lw=1.0):
    return np.full((n, n), ros), np.full((n, n), direction), np.full((n, n), lw)


def shape_error(neigh, lw=1.0, t=1200.0, cell=30.0, ros=2.0):
    """Largest shortfall of the simulated front against the ellipse over 72 directions, and the area error."""
    r, d, l = uniform(lw=lw, direction=90.0, ros=ros)
    e = np.sqrt(lw ** 2 - 1) / lw
    start = (200, int(200 - 0.5 * ros * t * 2 * e / (1 + e) / cell))        # centre the ellipse in the grid
    a = ss.arrival(r, d, l, cell, [start], t, neigh)
    rr, cc = np.nonzero(np.isfinite(a))
    ang = np.rad2deg(np.arctan2(cc - start[1], -(rr - start[0]))) % 360
    dist = np.hypot(rr - start[0], cc - start[1]) * cell
    worst = 0.0
    for k in range(72):
        m = (ang >= k * 5) & (ang < k * 5 + 5)
        want = ros * t * (1 - e) / (1 - e * np.cos(np.deg2rad(k * 5 + 2.5 - 90)))
        if want > 25 * cell:
            worst = min(worst, (dist[m].max() if m.any() else 0.0) / want - 1)
    head_run = ros * t
    return worst, float(np.isfinite(a).sum() * cell ** 2 / (np.pi * (head_run / (1 + e)) ** 2 / lw) - 1)


def test_head_matches_rothermel():
    r = np.random.default_rng(0)
    n = 5000
    g = dict(code=r.choice(sorted(ro.FBFM40) + [91, 99], n), vs=r.uniform(0, 15, n), th=r.uniform(0, 360, n), rmin=r.uniform(5, 60, n),
             tmmx=r.uniform(285, 315, n), fm100=r.uniform(4, 20, n), lfmc=r.uniform(50, 200, n), slope=r.uniform(0, 40, n),
             aspect=r.uniform(0, 360, n), green=r.uniform(0, 1, n), canopy_cover=r.uniform(0, 90, n), canopy_height=r.uniform(0, 30, n),
             fuel_state=r.uniform(0.3, 1.2, n))
    ref = ro.grid_layers(**g)
    c = ss.components(g["code"], g["rmin"], g["tmmx"], g["fm100"], g["lfmc"], g["green"], g["canopy_cover"], g["canopy_height"], g["fuel_state"])
    got = ss.head(c, g["vs"], g["th"], g["slope"], g["aspect"])
    for k in ("rate_of_spread", "flame_length", "fireline_intensity"):
        m = ref[k] > 1e-3
        assert np.abs(got[k][m] / ref[k][m] - 1).max() < 0.005
        assert ((ref[k] == 0) == (got[k] == 0)).all()


def test_no_wind_is_a_circle():
    for neigh, tol in ((8, 0.11), (16, 0.04), (32, 0.03)):
        front, total = shape_error(neigh)
        assert -tol < front <= 0.01 and -tol < total < 0.01


def test_wind_gives_the_ellipse():
    ros, d, l = uniform(lw=2.0, direction=90.0)
    a = ss.arrival(ros, d, l, 30.0, [(200, 100)], 1500.0, 32)
    reached = np.isfinite(a)
    cols, rows = np.nonzero(reached.any(axis=0))[0], np.nonzero(reached.any(axis=1))[0]
    e = np.sqrt(3) / 2
    assert abs((cols.max() - 100) * 30 / (2.0 * 1500) - 1) < 0.02                              # head run
    assert abs((100 - cols.min()) * 30 / (2.0 * 1500 * (1 - e) / (1 + e)) - 1) < 0.15          # backing run
    length, width = (cols.max() - cols.min()) * 30, (rows.max() - rows.min()) * 30
    assert abs(length / width / 2.0 - 1) < 0.08                                                # length to width
    for neigh, tol in ((16, 0.17), (32, 0.08)):
        assert -tol < shape_error(neigh, 2.0)[1] < 0.01


def test_barrier_stops_or_is_passed_round():
    ros, d, l = uniform()
    ros[:, 250:253] = 0                                    # a wall across the whole grid
    a = ss.arrival(ros, d, l, 30.0, [(200, 200)], 1e9, 16)
    assert np.isfinite(a[:, :250]).all() and not np.isfinite(a[:, 253:]).any()
    ros[380:, 250:253] = 2.0                               # a gap at the bottom
    b = ss.arrival(ros, d, l, 30.0, [(200, 200)], 1e9, 16)
    assert np.isfinite(b[200, 260]) and b[200, 260] > 5 * (60 * 30 / 2.0)     # reached only by going round
    one = np.where(np.arange(401)[None, :] == 251, 0.0, 2.0) * np.ones((401, 1))
    assert not np.isfinite(ss.arrival(one, d, l, 30.0, [(200, 200)], 1e9, 16)[200, 260])
    assert np.isfinite(ss.arrival(one, d, l, 30.0, [(200, 200)], 1e9, 16, gap_cells=2)[200, 260])   # a road is bridged


def test_start_on_road_and_permeability():
    ros, d, l = uniform()
    ros[200, 200] = 0
    a = ss.arrival(ros, d, l, 30.0, [(200, 200)], 300.0, 8)
    assert np.isfinite(a).sum() > 500
    ros, d, l = uniform()
    perm = np.ones_like(ros)
    perm[:, 210:] = 0.25
    slow = ss.arrival(ros, d, l, 30.0, [(200, 200)], 300.0, 16, permeability=perm)
    fast = ss.arrival(ros, d, l, 30.0, [(200, 200)], 300.0, 16)
    assert np.isfinite(slow).sum() < np.isfinite(fast).sum()
    assert np.array_equal(np.isfinite(slow[:, :200]), np.isfinite(fast[:, :200]))
    assert np.isfinite(fast[200, 210:]).sum() > 2 * np.isfinite(slow[200, 210:]).sum() > 0


def test_isochrones():
    from rasterio.transform import from_origin
    ros, d, l = uniform(n=201)
    a = ss.arrival(ros, d, l, 30.0, [(100, 100)], 60.0, 16)
    fc = ss.isochrones(a, [0.5, 1], from_origin(330000, 3780000, 30, 30), "EPSG:32611")
    big, small = fc["features"]
    assert big["properties"]["hours"] == 1 and small["properties"]["area_ha"] < big["properties"]["area_ha"]
    assert abs(big["properties"]["area_ha"] / (np.pi * 120 ** 2 / 1e4) - 1) < 0.05
    lon, lat = big["geometry"]["coordinates"][0][0][0]
    assert -119.9 < lon < -118 and 33.9 < lat < 34.3
