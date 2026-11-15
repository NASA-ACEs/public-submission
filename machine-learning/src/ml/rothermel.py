"""Rothermel (1972) surface fire spread for the 40 Scott & Burgan (2005) fuel models. numpy only, vectorised.

Equations: Rothermel 1972 (INT-115) with the Albini 1976 revisions, as documented in Andrews 2018 (RMRS-GTR-371);
dynamic herbaceous load transfer from Scott & Burgan 2005 / Burgan 1979; wind and slope combined as vectors on
the slope plane and the effective wind speed limited to 0.9 * reaction intensity (Rothermel 1972 eq. 86), the
same choices as BehavePlus and pyretechnics (which test_rothermel.py checks against). Flame length: Byram 1959.
Fuel parameters: Scott & Burgan 2005, RMRS-GTR-153, Table 7 (copied from fuel-lake config/tables/fbfm40_parameters.csv).

Surface fire only: no crown fire, no spotting. Wind is a single midflame value per cell.
"""
import numpy as np

T_AC = 2000.0 / 43560.0          # short ton/acre -> lb/ft2
FT = 0.3048                      # ft -> m
BTU_FT_S = 3.46165186            # Btu/ft/s -> kW/m
RHO_P, S_T, S_E = 32.0, 0.0555, 0.01
SAV_10H, SAV_100H = 109.0, 30.0
ETA_S = 0.174 * S_E ** -0.19

# code: (name, depth ft, dead moisture of extinction %, heat content Btu/lb,
#        loads t/ac: 1-h, 10-h, 100-h, live herb, live woody,  SAV 1/ft: 1-h, live herb, live woody)
FBFM40 = {
    101: ('GR1', 0.4, 15, 8000, 0.1, 0.0, 0.0, 0.3, 0.0, 2200, 2000, 0),
    102: ('GR2', 1.0, 15, 8000, 0.1, 0.0, 0.0, 1.0, 0.0, 2000, 1800, 0),
    103: ('GR3', 2.0, 30, 8000, 0.1, 0.4, 0.0, 1.5, 0.0, 1500, 1300, 0),
    104: ('GR4', 2.0, 15, 8000, 0.25, 0.0, 0.0, 1.9, 0.0, 2000, 1800, 0),
    105: ('GR5', 1.5, 40, 8000, 0.4, 0.0, 0.0, 2.5, 0.0, 1800, 1600, 0),
    106: ('GR6', 1.5, 40, 9000, 0.1, 0.0, 0.0, 3.4, 0.0, 2200, 2000, 0),
    107: ('GR7', 3.0, 15, 8000, 1.0, 0.0, 0.0, 5.4, 0.0, 2000, 1800, 0),
    108: ('GR8', 4.0, 30, 8000, 0.5, 1.0, 0.0, 7.3, 0.0, 1500, 1300, 0),
    109: ('GR9', 5.0, 40, 8000, 1.0, 1.0, 0.0, 9.0, 0.0, 1800, 1600, 0),
    121: ('GS1', 0.9, 15, 8000, 0.2, 0.0, 0.0, 0.5, 0.65, 2000, 1800, 1800),
    122: ('GS2', 1.5, 15, 8000, 0.5, 0.5, 0.0, 0.6, 1.0, 2000, 1800, 1800),
    123: ('GS3', 1.8, 40, 8000, 0.3, 0.25, 0.0, 1.45, 1.25, 1800, 1600, 1600),
    124: ('GS4', 2.1, 40, 8000, 1.9, 0.3, 0.1, 3.4, 7.1, 1800, 1600, 1600),
    141: ('SH1', 1.0, 15, 8000, 0.25, 0.25, 0.0, 0.15, 1.3, 2000, 1800, 1600),
    142: ('SH2', 1.0, 15, 8000, 1.35, 2.4, 0.75, 0.0, 3.85, 2000, 0, 1600),
    143: ('SH3', 2.4, 40, 8000, 0.45, 3.0, 0.0, 0.0, 6.2, 1600, 0, 1400),
    144: ('SH4', 3.0, 30, 8000, 0.85, 1.15, 0.2, 0.0, 2.55, 2000, 1800, 1600),
    145: ('SH5', 6.0, 15, 8000, 3.6, 2.1, 0.0, 0.0, 2.9, 750, 0, 1600),
    146: ('SH6', 2.0, 30, 8000, 2.9, 1.45, 0.0, 0.0, 1.4, 750, 0, 1600),
    147: ('SH7', 6.0, 15, 8000, 3.5, 5.3, 2.2, 0.0, 3.4, 750, 0, 1600),
    148: ('SH8', 3.0, 40, 8000, 2.05, 3.4, 0.85, 0.0, 4.35, 750, 0, 1600),
    149: ('SH9', 4.4, 40, 8000, 4.5, 2.45, 0.0, 1.55, 7.0, 750, 1800, 1500),
    161: ('TU1', 0.6, 20, 8000, 0.2, 0.9, 1.5, 0.2, 0.9, 2000, 1800, 1600),
    162: ('TU2', 1.0, 30, 8000, 0.95, 1.8, 1.25, 0.0, 0.2, 2000, 0, 1600),
    163: ('TU3', 1.3, 30, 8000, 1.1, 0.15, 0.25, 0.65, 1.1, 1800, 1600, 1400),
    164: ('TU4', 0.5, 12, 8000, 4.5, 0.0, 0.0, 0.0, 2.0, 2300, 0, 2000),
    165: ('TU5', 1.0, 25, 8000, 4.0, 4.0, 3.0, 0.0, 3.0, 1500, 0, 750),
    181: ('TL1', 0.2, 30, 8000, 1.0, 2.2, 3.6, 0.0, 0.0, 2000, 0, 0),
    182: ('TL2', 0.2, 25, 8000, 1.4, 2.3, 2.2, 0.0, 0.0, 2000, 0, 0),
    183: ('TL3', 0.3, 20, 8000, 0.5, 2.2, 2.8, 0.0, 0.0, 2000, 0, 0),
    184: ('TL4', 0.4, 25, 8000, 0.5, 1.5, 4.2, 0.0, 0.0, 2000, 0, 0),
    185: ('TL5', 0.6, 25, 8000, 1.15, 2.5, 4.4, 0.0, 0.0, 2000, 0, 1600),
    186: ('TL6', 0.3, 25, 8000, 2.4, 1.2, 1.2, 0.0, 0.0, 2000, 0, 0),
    187: ('TL7', 0.4, 25, 8000, 0.3, 1.4, 8.1, 0.0, 0.0, 2000, 0, 0),
    188: ('TL8', 0.3, 35, 8000, 5.8, 1.4, 1.1, 0.0, 0.0, 1800, 0, 0),
    189: ('TL9', 0.6, 35, 8000, 6.65, 3.3, 4.15, 0.0, 0.0, 1800, 0, 1600),
    201: ('SB1', 1.0, 25, 8000, 1.5, 3.0, 11.0, 0.0, 0.0, 2000, 0, 0),
    202: ('SB2', 1.0, 25, 8000, 4.5, 4.25, 4.0, 0.0, 0.0, 2000, 0, 0),
    203: ('SB3', 1.2, 25, 8000, 5.5, 2.75, 3.0, 0.0, 0.0, 2000, 0, 0),
    204: ('SB4', 2.7, 25, 8000, 5.25, 3.5, 5.25, 0.0, 0.0, 2000, 0, 0),
}
NAMES = {c: v[0] for c, v in FBFM40.items()}
_P = np.full((256, 11), np.nan)
for _c, _v in FBFM40.items():
    _P[_c] = _v[1:]
_P[[91, 92, 93, 98, 99]] = 0.0   # non-burnable: no spread


def emc(rh, temp_c):
    """Equilibrium moisture content, % (Simard 1968, as used by the 1978 NFDRS: Bradshaw et al. 1984)."""
    rh, t = np.asarray(rh, "float64"), np.asarray(temp_c, "float64") * 1.8 + 32
    return np.where(rh < 10, 0.03229 + 0.281073 * rh - 0.000578 * rh * t,
                    np.where(rh < 50, 2.22749 + 0.160107 * rh - 0.014784 * t,
                             21.0606 + 0.005565 * rh ** 2 - 0.00035 * rh * t - 0.483199 * rh))


def dead_moisture(rmin, tmmx_k):
    """1-h and 10-h dead fuel moisture, %, at the driest hour: 1.03 and 1.28 x EMC (Bradshaw et al. 1984)."""
    e = np.clip(emc(rmin, np.asarray(tmmx_k, "float64") - 273.15), 1.0, 60.0)
    return 1.03 * e, 1.28 * e


def midflame_wind(wind_10m, code, canopy_cover=None, canopy_height=None):
    """Midflame wind, m/s, from 10 m wind: 20-ft = 10 m / 1.15 (Turner & Lawson 1978), then the wind adjustment
    factor of Andrews 2012 (RMRS-GTR-266): unsheltered from fuel bed depth; sheltered when the canopy fills
    >= 5 % of the space (cover/3) and is >= 6 ft high. canopy_cover in %, canopy_height in m; None = unsheltered."""
    depth = _P[_codes(code), 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        waf = np.where(depth > 0, 1.83 / np.log((20 + 0.36 * depth) / (0.13 * depth)), 0.0)
        if canopy_cover is not None and canopy_height is not None:
            f = np.nan_to_num(np.asarray(canopy_cover, "float64")) / 100 / 3
            h = np.nan_to_num(np.asarray(canopy_height, "float64")) / FT
            shel = (f >= 0.05) & (h >= 6.0)
            hs = np.where(shel, h, 10.0)
            waf = np.where(shel & (depth > 0), 0.555 / (np.sqrt(np.where(shel, f, 1.0) * hs) * np.log((20 + 0.36 * hs) / (0.13 * hs))), waf)
    return np.asarray(wind_10m, "float64") / 1.15 * waf


def herb_moisture(green):
    """Live herbaceous moisture, %, from a greenness fraction 0-1: 30 % (fully cured) to 120 % (uncured),
    the range over which Scott & Burgan's dynamic models transfer herbaceous load to dead fuel."""
    return 30.0 + 90.0 * np.clip(green, 0.0, 1.0)


def _codes(code):
    c = np.asarray(code, "float64")
    return np.where(np.isfinite(c) & (c >= 0) & (c < 256), c, 0).astype(np.int64)


def bed(code, m_1h, m_10h, m_100h, m_herb, m_woody, fuel_state=1.0):
    """The part of spread() that does not depend on wind or slope, for flat (already broadcast, 1-D) inputs:
    r0 (ft/min, no wind, flat), i_r (Btu/ft2/min), i0 (Btu/ft/s at r0), c_f and b of the wind factor
    phi_w = c_f * u**b (u midflame, ft/min), beta (packing ratio), and the masks ok / known."""
    code, m1, m10, m100, mh, mw, state = [np.asarray(a, "float64").ravel() for a in (code, m_1h, m_10h, m_100h, m_herb, m_woody, fuel_state)]
    p = _P[_codes(code)]
    state = np.where((p[:, 7] > 0) & np.isfinite(state), state, 1.0)[:, None]
    p = np.hstack([p[:, :1] * state, p[:, 1:3], p[:, 3:6] * state, p[:, 6:7], p[:, 7:8] * state, p[:, 8:]])
    delta, mxd, heat = p[:, 0], p[:, 1] / 100, p[:, 2]
    green = np.clip(mh / 100 / 0.9 - 1 / 3, 0, 1)                       # dynamic transfer (Burgan 1979)
    herb = p[:, 6] * T_AC
    w = np.stack([p[:, 3] * T_AC, p[:, 4] * T_AC, p[:, 5] * T_AC, herb * (1 - green), herb * green, p[:, 7] * T_AC], 1)
    sig = np.stack([p[:, 8], np.full(len(p), SAV_10H), np.full(len(p), SAV_100H), p[:, 9], p[:, 9], p[:, 10]], 1)
    sig = np.where(w > 0, sig, 0.0)
    m = np.stack([m1, m10, m100, m1, mh, mw], 1) / 100
    dead = np.array([True, True, True, True, False, False])
    with np.errstate(divide="ignore", invalid="ignore"):
        a = sig * w / RHO_P
        a_d, a_l = a[:, dead].sum(1), a[:, ~dead].sum(1)
        a_t = a_d + a_l
        f = np.where(dead, a / a_d[:, None], a / a_l[:, None])
        f = np.where(np.isfinite(f), f, 0.0)
        f_d, f_l = np.nan_to_num(a_d / a_t), np.nan_to_num(a_l / a_t)
        # Albini 1976: loads of one size class within a category are pooled for the net load
        cls = np.digitize(sig, [16, 48, 96, 192, 1200])
        g = np.zeros_like(f)
        for cat in (dead, ~dead):
            for k in range(6):
                s = np.where(cls[:, cat] == k, f[:, cat], 0.0).sum(1)
                g[:, cat] += np.where(cls[:, cat] == k, s[:, None], 0.0)
        # live moisture of extinction (Rothermel 1972 eq. 88, Albini 1976)
        lf = np.where(sig > 0, w * np.exp(np.where(dead, -138.0, -500.0) / np.where(sig > 0, sig, 1.0)), 0.0)
        d_l, l_l = lf[:, dead].sum(1), lf[:, ~dead].sum(1)
        m_dead = (m[:, dead] * lf[:, dead]).sum(1) / d_l
        mxl = np.where((d_l > 0) & (l_l > 0), np.maximum(mxd, 2.9 * d_l / l_l * (1 - m_dead / mxd) - 0.226), mxd)
        mx = np.where(dead, mxd[:, None], mxl[:, None])
        sigma = f_d * (f[:, dead] * sig[:, dead]).sum(1) + f_l * (f[:, ~dead] * sig[:, ~dead]).sum(1)
        beta = w.sum(1) / RHO_P / delta
        ratio = beta / (3.348 * sigma ** -0.8189)

        def eta_m(cat):
            r = np.minimum(1.0, (f[:, cat] * m[:, cat]).sum(1) / (f[:, cat] * mx[:, cat]).sum(1))
            return np.where(r < 1, np.nan_to_num(1 - 2.59 * r + 5.11 * r ** 2 - 3.52 * r ** 3), 0.0)

        w_n = lambda cat: (g[:, cat] * w[:, cat]).sum(1) * (1 - S_T)
        gamma_a = 133.0 * sigma ** -0.7913
        gamma = sigma ** 1.5 / (495 + 0.0594 * sigma ** 1.5) * ratio ** gamma_a * np.exp(gamma_a * (1 - ratio))
        i_r = gamma * heat * ETA_S * (w_n(dead) * eta_m(dead) + w_n(~dead) * eta_m(~dead))      # Btu/ft2/min
        xi = np.exp((0.792 + 0.681 * np.sqrt(sigma)) * (beta + 0.1)) / (192 + 0.2595 * sigma)
        eps = np.where(sig > 0, np.exp(-138.0 / np.where(sig > 0, sig, 1.0)), 0.0)
        q_ig = np.where(m > 0, 250 + 1116 * m, 0.0)
        sink = w.sum(1) / delta * (f_d * (f[:, dead] * eps[:, dead] * q_ig[:, dead]).sum(1)
                                   + f_l * (f[:, ~dead] * eps[:, ~dead] * q_ig[:, ~dead]).sum(1))
        r0 = i_r * xi / sink                                                                    # ft/min
        i0 = i_r * r0 * 384 / sigma / 60                                                        # Btu/ft/s
        b = 0.02526 * sigma ** 0.54
        c_f = 7.47 * np.exp(-0.133 * sigma ** 0.55) * ratio ** -(0.715 * np.exp(-3.59e-4 * sigma))
    ok = np.isfinite(r0) & (sink > 0) & (delta > 0)
    known = np.isfinite(p[:, 0]) & np.isfinite(code)
    return dict(r0=r0, i_r=i_r, i0=i0, c_f=c_f, b=b, beta=beta, ok=ok, known=known)


def spread(code, m_1h, m_10h, m_100h, m_herb, m_woody, wind, wind_from=0.0, slope=0.0, aspect=0.0, wind_limit=True,
           fuel_state=1.0):
    """Head fire behaviour per cell.

    code: FBFM40 number. m_*: fuel moisture, % of dry weight (m_woody = LFMC). wind: midflame wind, m/s.
    wind_from: degrees clockwise from north the wind blows from. slope: degrees. aspect: degrees, downslope direction.
    fuel_state: woody fuel present relative to the fuel model (1 = as tabulated). In models with live woody fuel it
    scales the dead and live woody loads and the bed depth together, i.e. a shorter, lighter stand of the same
    bulk density; NaN counts as 1.
    Returns dict of float64 arrays: ros (m/min), intensity (kW/m), flame_length (m), direction (degrees the head
    fire moves towards), ros_no_wind (m/min, flat and calm), lw_ratio (length-to-width of the fire ellipse).
    Non-burnable codes give 0, unknown codes NaN.
    """
    arrs = np.broadcast_arrays(*[np.asarray(a, "float64") for a in (code, m_1h, m_10h, m_100h, m_herb, m_woody, wind, wind_from, slope, aspect, fuel_state)])
    shape = arrs[0].shape
    code, m1, m10, m100, mh, mw, wind, wfrom, slope, aspect, state = [a.ravel() for a in arrs]
    f = bed(code, m1, m10, m100, mh, mw, state)
    r0, i_r, i0, c_f, b, beta, ok, known = (f[k] for k in ("r0", "i_r", "i0", "c_f", "b", "beta", "ok", "known"))
    with np.errstate(divide="ignore", invalid="ignore"):
        # wind and slope as vectors on the slope plane
        tan = np.tan(np.deg2rad(np.nan_to_num(slope)))
        az_w, az_s = np.deg2rad(wfrom + 180), np.deg2rad(np.nan_to_num(aspect) + 180)
        u = wind * 60 / FT                                                                      # ft/min
        wx, wy, sx, sy = u * np.sin(az_w), u * np.cos(az_w), tan * np.sin(az_s), tan * np.cos(az_s)
        wz = wx * sx + wy * sy
        u3 = np.sqrt(wx ** 2 + wy ** 2 + wz ** 2)
        phi_w, phi_s = c_f * u3 ** b, 5.275 * beta ** -0.3 * tan ** 2
        s3 = np.sqrt(sx ** 2 + sy ** 2 + tan ** 4)
        kw, ks = np.where(u3 > 0, phi_w / u3, 0.0), np.where(s3 > 0, phi_s / s3, 0.0)
        ex, ey, ez = kw * wx + ks * sx, kw * wy + ks * sy, kw * wz + ks * tan ** 2
        phi = np.sqrt(ex ** 2 + ey ** 2 + ez ** 2)
        direction = np.where(phi > 0, np.round(np.rad2deg(np.arctan2(ex, ey)), 9) % 360, 0.0)
        u_eff = (phi / c_f) ** (1 / b)                                                          # ft/min
        if wind_limit:
            over = u_eff > 0.9 * i_r
            phi = np.where(over, c_f * (0.9 * i_r) ** b, phi)
            u_eff = np.where(over, 0.9 * i_r, u_eff)
        mph = np.nan_to_num(u_eff) * FT * 0.0372840909091
        lw = np.minimum(8.0, 0.936 * np.exp(0.1147 * mph) + 0.461 * np.exp(-0.0692 * mph) - 0.397)
    out = {"ros": r0 * (1 + phi) * FT, "intensity": i0 * (1 + phi) * BTU_FT_S, "ros_no_wind": r0 * FT, "direction": direction, "lw_ratio": lw}
    out = {k: np.where(known, np.where(ok, np.nan_to_num(v), 1.0 if k == "lw_ratio" else 0.0), np.nan) for k, v in out.items()}
    out["flame_length"] = 0.07747042253266703 * out["intensity"] ** 0.46
    return {k: v.reshape(shape) for k, v in out.items()}


def grid_layers(code, vs, th, rmin, tmmx, fm100, lfmc, slope, aspect, green=0.33, canopy_cover=None, canopy_height=None,
                fuel_state=1.0, chunk=1_000_000):
    """Map layers from arrays on one grid (any shape): fuel code, gridMET day (vs m/s at 10 m, th degrees, rmin %,
    tmmx K, fm100 %), LFMC %, slope and aspect in degrees, herbaceous greenness 0-1 (scalar or array), fuel_state as
    in spread().
    Returns float32 rate_of_spread (m/min), flame_length (m), fireline_intensity (kW/m), spread_direction (degrees)."""
    shape = np.shape(code)
    flat = lambda a: None if a is None else np.broadcast_to(np.asarray(a), shape).ravel()
    code, vs, th, rmin, tmmx, fm100, lfmc, slope, aspect, green, cc, ch, state = map(flat, (code, vs, th, rmin, tmmx, fm100, lfmc, slope, aspect, green, canopy_cover, canopy_height, fuel_state))
    names = {"ros": "rate_of_spread", "flame_length": "flame_length", "intensity": "fireline_intensity", "direction": "spread_direction"}
    out = {v: np.empty(code.size, "float32") for v in names.values()}
    for i in range(0, code.size, chunk):
        s = slice(i, i + chunk)
        m1, m10 = dead_moisture(rmin[s], tmmx[s])
        wind = midflame_wind(vs[s], code[s], None if cc is None else cc[s], None if ch is None else ch[s])
        r = spread(code[s], m1, m10, fm100[s], herb_moisture(green[s]), lfmc[s], wind, th[s], slope[s], aspect[s], fuel_state=state[s])
        for k, v in names.items():
            out[v][s] = r[k]
    return {k: v.reshape(shape) for k, v in out.items()}
