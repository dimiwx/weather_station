#!/usr/bin/env python3
"""
Builds the 1991-2020 climatology for Kalamaria that the dashboard draws behind its charts and in the History tab.

Run it by hand (a few minutes; downloads are cached in climatology/raw/, which git ignores). Re-run it only to
change the period, the sources or the light factor.

Sources (Open-Meteo's archive API, hourly, the grid cell nearest 40.58 N, 22.95 E):
  - 2 m temperature and 2 m relative humidity: ERA5-Land (0.1°, cell 40.6 N 23.0 E). Compared with the station
    (MCP9808, 2025-03 to 2026-11) it has the right daily cycle (6.2 °C average daily range against the station's
    6.8 °C) and a steady offset (the station is ~1.2 °C warmer in every month, town and siting). ERA5 (0.25°) was
    rejected for temperature: its cell (40.5 N 23.0 E) is half sea, its daily range is only 4.6 °C, and its bias
    swings with the season (-0.5 °C in winter, +1.3 °C in summer).
  - mean sea-level pressure, 10 m wind speed and shortwave (sunlight) radiation: ERA5 (ERA5-Land has none of
    them on Open-Meteo). The station's pressure reads ~0.6 hPa below ERA5's sea-level pressure.
  - light: ERA5 shortwave radiation x LUX_PER_WM2. The station's light sensor (VEML7700) reads ~60 lux per W/m²
    of ERA5 sunlight (all-sky average, 2025-03 to 2026-11), about half of real daylight's ~110-120 lux per W/m²,
    so the climatology is put on the sensor's scale and stays comparable with its readings.
  - UV index: not yet (see CLAUDE.md).

Output (UTC):
  climatology/kalamaria_hourly.csv  one row per calendar day and hour: doy (1-366, numbered as in a leap year,
                                    so 29 Feb is 60 and 31 Dec is 366), hour (UTC), then <var>_mean, <var>_p10,
                                    <var>_p90 for each variable: hourly values of the 30 years within 15 days
                                    either side of the date (31 x 30 = 930 values per day and hour).
  climatology/kalamaria_daily.csv   one row per calendar day: the same statistics of daily (UTC) means.
  climatology/kalamaria_meta.json   period, sources, grid cells and settings.
"""
import json
import os
import sys
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "climatology")
RAW_DIR = os.path.join(OUT_DIR, "raw")
LAT, LON = 40.58, 22.95          # Kalamaria (Krini)
FIRST_YEAR, LAST_YEAR = 1991, 2020
WINDOW_DAYS = 15                 # days either side of each calendar day
LUX_PER_WM2 = 60.0               # the station's light sensor against ERA5 sunlight (see above)
API = "https://archive-api.open-meteo.com/v1/archive"
# model: [(Open-Meteo variable, our name)]
SOURCES = {
    "era5_land": [("temperature_2m", "t"), ("relative_humidity_2m", "rh")],
    "era5": [("pressure_msl", "p"), ("wind_speed_10m", "ws"), ("shortwave_radiation", "sw")],
}
VARS = ["t", "rh", "p", "ws", "lux"]
DECIMALS = {"t": 2, "rh": 1, "p": 2, "ws": 2, "lux": 0}


def download(model, year):
    """One year of one model, cached as JSON in climatology/raw/."""
    path = os.path.join(RAW_DIR, f"{model}_{year}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    params = {"latitude": LAT, "longitude": LON, "start_date": f"{year}-01-01", "end_date": f"{year}-12-31",
              "hourly": ",".join(v for v, _ in SOURCES[model]), "models": model, "timezone": "GMT",
              "wind_speed_unit": "ms"}
    for attempt in range(6):
        r = requests.get(API, params=params, timeout=120)
        if r.status_code == 200:
            break
        wait = 60 * (attempt + 1) if r.status_code == 429 else 10
        print(f"  {model} {year}: HTTP {r.status_code}, retrying in {wait} s")
        time.sleep(wait)
    r.raise_for_status()
    data = r.json()
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    time.sleep(1)   # be gentle with the free API
    return data


def leap_doy_index(index):
    """0-365: the day's position in a leap year, so 1 March is always 60 (0-based)."""
    return np.array([(date(2000, m, d) - date(2000, 1, 1)).days for m, d in zip(index.month, index.day)])


def window_stats(arr):
    """arr: (years, 366, ...) -> mean, p10, p90 per calendar day over the years and +-WINDOW_DAYS days."""
    out = [np.empty((366,) + arr.shape[2:]) for _ in range(3)]
    for d in range(366):
        win = [(d + o) % 366 for o in range(-WINDOW_DAYS, WINDOW_DAYS + 1)]
        block = arr[:, win].reshape((-1,) + arr.shape[2:])
        out[0][d] = np.nanmean(block, axis=0)
        out[1][d], out[2][d] = np.nanpercentile(block, [10, 90], axis=0)
    return out


def main():
    frames, cells = [], {}
    for model in SOURCES:
        parts = []
        for year in range(FIRST_YEAR, LAST_YEAR + 1):
            data = download(model, year)
            cells[model] = {"latitude": data["latitude"], "longitude": data["longitude"], "elevation": data["elevation"]}
            h = pd.DataFrame(data["hourly"]).set_index("time")
            parts.append(h)
            print(f"\r{model} {year}", end="", flush=True)
        print()
        df = pd.concat(parts)
        df.index = pd.to_datetime(df.index)
        frames.append(df.rename(columns=dict(SOURCES[model])).apply(pd.to_numeric, errors="coerce"))
    df = pd.concat(frames, axis=1).sort_index()
    df = df[~df.index.duplicated()]
    # Open-Meteo's radiation is the mean of the preceding hour; average neighbours for the value at the hour
    df["lux"] = (df["sw"] + df["sw"].shift(-1)) / 2 * LUX_PER_WM2
    missing = df[VARS].isna().mean()
    print("missing share:", missing.round(4).to_dict())

    years = df.index.year - FIRST_YEAR
    doy = leap_doy_index(df.index)
    hours = df.index.hour
    hourly = {"doy": np.repeat(np.arange(1, 367), 24), "hour": np.tile(np.arange(24), 366)}
    daily = {"doy": np.arange(1, 367)}
    n_years = LAST_YEAR - FIRST_YEAR + 1
    for v in VARS:
        arr = np.full((n_years, 366, 24), np.nan)
        arr[years, doy, hours] = df[v].values
        complete = ~np.isnan(arr).any(axis=2)
        day_means = np.where(complete, np.nanmean(np.where(complete[..., None], arr, 0), axis=2), np.nan)
        for name, stat in zip(("mean", "p10", "p90"), window_stats(arr)):
            hourly[f"{v}_{name}"] = stat.reshape(-1).round(DECIMALS[v])
        for name, stat in zip(("mean", "p10", "p90"), window_stats(day_means)):
            daily[f"{v}_{name}"] = stat.round(DECIMALS[v])
        print(f"{v}: done")

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(hourly).to_csv(os.path.join(OUT_DIR, "kalamaria_hourly.csv"), index=False)
    pd.DataFrame(daily).to_csv(os.path.join(OUT_DIR, "kalamaria_daily.csv"), index=False)
    meta = {"period": f"{FIRST_YEAR}-{LAST_YEAR}", "location": {"latitude": LAT, "longitude": LON},
            "window_days": WINDOW_DAYS, "lux_per_wm2": LUX_PER_WM2, "built": datetime.utcnow().strftime("%Y-%m-%d"),
            "sources": {"t": "era5_land", "rh": "era5_land", "p": "era5", "ws": "era5", "lux": "era5"},
            "grid_cells": cells, "units": {"t": "°C", "rh": "%", "p": "hPa (sea level)", "ws": "m/s (10 m)",
                                            "lux": "lux (station sensor scale)"}}
    with open(os.path.join(OUT_DIR, "kalamaria_meta.json"), "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    print("written to", OUT_DIR)


if __name__ == "__main__":
    sys.exit(main())
