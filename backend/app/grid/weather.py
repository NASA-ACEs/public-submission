"""Hourly 24h wind / humidity forecast from NOAA NWS, with a deterministic baseline fallback."""
import math
import re

import httpx

COMPASS = {d: i * 22.5 for i, d in enumerate(
    'N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW'.split())}
HEADERS = {'Accept': 'application/geo+json', 'User-Agent': 'nasa-aces-grid-risk (research prototype)'}


def _period(i, wind_mph, label, rh, temp_c):
    return dict(hour=i, windMph=round(wind_mph, 1), windDirLabel=label,
                spreadBearingDeg=(COMPASS.get(label, 270) + 180) % 360, rhPct=round(rh), tempC=round(temp_c))


def _bundle(source, periods):
    dominant = periods[min(6, len(periods) - 1)]
    return dict(source=source, periods=periods,
                avgWindMph=round(sum(p['windMph'] for p in periods) / len(periods), 1),
                dominantWindLabel=dominant['windDirLabel'], minRhPct=min(p['rhPct'] for p in periods),
                maxTempC=max(p['tempC'] for p in periods))


def baseline():
    periods = []
    for h in range(25):
        diurnal = math.sin((h + 4) / 24 * math.pi * 2)
        periods.append(_period(h, 10 + 4 * max(0, diurnal), 'SW', 34 - 10 * diurnal, 24 + 5 * diurnal))
    return _bundle('Baseline', periods)


def _wind(raw):
    nums = [float(n) for n in re.findall(r'\d+(?:\.\d+)?', str(raw or ''))]
    return sum(nums) / len(nums) if nums else 9


def fetch(client: httpx.Client, lon, lat):
    try:
        point = client.get(f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}', headers=HEADERS)
        point.raise_for_status()
        hourly_url = point.json()['properties']['forecastHourly']
        hourly = client.get(hourly_url, headers=HEADERS)
        hourly.raise_for_status()
        raw = hourly.json()['properties']['periods'][:25]
        if not raw:
            return baseline()
        periods = []
        for i, p in enumerate(raw):
            temp = float(p.get('temperature') or 75)
            temp_c = temp if p.get('temperatureUnit') == 'C' else (temp - 32) * 5 / 9
            rh = (p.get('relativeHumidity') or {}).get('value')
            periods.append(_period(i, _wind(p.get('windSpeed')), str(p.get('windDirection') or 'W').strip().upper(),
                                   max(5, min(100, rh if rh is not None else 32)), temp_c))
        while len(periods) < 25:
            periods.append(dict(periods[-1], hour=len(periods)))
        return _bundle('NOAA NWS', periods)
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return baseline()
