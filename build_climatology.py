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
  - UV index (2016-2025, a shorter period: no reanalysis has UV, and Open-Meteo has no historical UV): CAMS
    global atmospheric composition forecasts from the Copernicus Atmosphere Data Store (ADS), the first 24 hours
    of each day's 00 UTC run, UV biologically effective dose x 40 = UV index, interpolated to the station from
    the ~0.4° grid. It needs the ADS key (the same as ~/.cdsapirc's) and the ADS terms and the CAMS licence
    accepted on the website. The data store fetches these forecasts from tape: one request per year (its limit
    is 10,000 fields a request; a year of 25 lead hours is 9,125), which can wait hours in its queue (the files are cached in climatology/raw/ and the request ids in
    cams_uv_jobs.json, so an interrupted run resumes; --skip-uv builds the rest without UV).

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
import shutil
import sys
import time
import zipfile
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
DECIMALS = {"t": 2, "rh": 1, "p": 2, "ws": 2, "lux": 0, "uv": 2}

UV_FIRST_YEAR, UV_LAST_YEAR = 2016, 2025
ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
ADS_DATASET = "cams-global-atmospheric-composition-forecasts"
UV_AREA = [41.0, 22.6, 40.2, 23.4]   # north, west, south, east: the grid points around the station
UV_JOBS = os.path.join(RAW_DIR, "cams_uv_jobs.json")   # request ids, so an interrupted run resumes
UV_QUEUE = 2                         # requests queued at the data store at once (its limit per dataset and user)
UV_PER_WM2 = 40.0                    # UV index per W/m² of erythemally weighted irradiance


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


def calendar_stats(series, first_year, last_year):
    """Hourly (366*24) and daily (366) mean, p10, p90 of a UTC-hourly series over first_year..last_year."""
    s = series[(series.index.year >= first_year) & (series.index.year <= last_year)]
    arr = np.full((last_year - first_year + 1, 366, 24), np.nan)
    arr[s.index.year - first_year, leap_doy_index(s.index), s.index.hour] = s.values
    complete = ~np.isnan(arr).any(axis=2)
    day_means = np.where(complete, np.nanmean(np.where(complete[..., None], arr, 0), axis=2), np.nan)
    return window_stats(arr), window_stats(day_means)


def ads_client():
    from ecmwf.datastores import Client   # installed with cdsapi
    with open(os.path.expanduser("~/.cdsapirc")) as f:
        key = next(line.split(":", 1)[1].strip() for line in f if line.startswith("key:"))
    return Client(url=ADS_URL, key=key)


def uv_year_path(year):
    return os.path.join(RAW_DIR, f"cams_uv_{year}.nc")


def uv_request(year):
    """A year of the 00 UTC runs' first 24 hours (one request: the data store accepts a year)."""
    return {"date": [f"{year}-01-01/{year}-12-31"], "time": ["00:00"], "type": ["forecast"],
            "leadtime_hour": [str(h) for h in range(0, 25)], "variable": ["uv_biologically_effective_dose"],
            "area": UV_AREA, "data_format": "netcdf_zip"}


def download_uv():
    """Downloads the missing years, keeping UV_QUEUE requests at the data store at a time (it rejects more:
    "Maximum number queued requests for this dataset has been reached"). Request ids are kept in UV_JOBS, so an
    interrupted run picks the same requests up again."""
    missing = [y for y in range(UV_FIRST_YEAR, UV_LAST_YEAR + 1) if not os.path.exists(uv_year_path(y))]
    if not missing:
        return
    os.makedirs(RAW_DIR, exist_ok=True)
    client = ads_client()
    jobs = {}
    if os.path.exists(UV_JOBS):
        with open(UV_JOBS) as f:
            jobs = json.load(f)

    def save():
        with open(UV_JOBS, "w") as f:
            json.dump(jobs, f, indent=1)

    print(f"CAMS UV: {len(missing)} years to download, {UV_QUEUE} at a time (each can wait hours)", flush=True)
    pending, failures, last_report, t0 = list(missing), {}, "", time.time()
    while pending:
        states = {}
        for y in [y for y in pending if str(y) in jobs]:
            remote = client.get_remote(jobs[str(y)])
            states[y] = remote.status
            if states[y] == "successful":
                path = uv_year_path(y)
                remote.download(path + ".zip")
                with zipfile.ZipFile(path + ".zip") as z:
                    name = next(n for n in z.namelist() if n.endswith(".nc"))
                    with z.open(name) as src, open(path + ".tmp", "wb") as dst:
                        shutil.copyfileobj(src, dst)
                os.replace(path + ".tmp", path)
                os.remove(path + ".zip")
                pending.remove(y)
                jobs.pop(str(y))
                save()
                print(f"  {datetime.now():%H:%M} {y}: downloaded ({(time.time() - t0) / 3600:.1f} h so far)", flush=True)
            elif states[y] in ("failed", "rejected", "dismissed", "deleted"):
                jobs.pop(str(y))
                save()
                try:
                    remote.get_results()   # raises with the data store's reason
                    reason = ""
                except Exception as e:
                    reason = " ".join(str(e).split())[:300]
                if "queued requests" not in reason:   # the queue limit only means "try again later"
                    failures[y] = failures.get(y, 0) + 1
                    print(f"  {y}: {states[y]} {reason}", flush=True)
                    if failures[y] >= 3:
                        print(f"  {y}: giving up after 3 failures", flush=True)
                        pending.remove(y)
        open_jobs = sum(1 for y in pending if str(y) in jobs)
        for y in [y for y in pending if str(y) not in jobs][:max(0, UV_QUEUE - open_jobs)]:
            try:
                jobs[str(y)] = client.submit(ADS_DATASET, uv_request(y)).request_id
                save()
                states[y] = "submitted"
            except Exception as e:
                print(f"  {y}: not submitted ({type(e).__name__}: {str(e)[:200]})", flush=True)
                break
        report = ", ".join(f"{y} {st}" for y, st in sorted(states.items()) if y in pending)
        if pending and report != last_report:
            print(f"  {datetime.now():%H:%M} {report}; {len(pending)} years to go", flush=True)
            last_report = report
        if pending:
            time.sleep(120)


def uv_file_series(path):
    """(UV index Series by valid time, accumulated?) at the station from one downloaded file. Interpolated
    bilinearly between the grid points; hours 1 to 24 of each run, so the days join without overlap."""
    import xarray as xr
    with xr.open_dataset(path) as ds:
        name = next(v for v in ds.data_vars if "uvb" in v.lower())
        da = ds[name].interp(latitude=LAT, longitude=LON)
        df = da.to_dataframe(name="v").reset_index()
    ref = "forecast_reference_time" if "forecast_reference_time" in df.columns else "time"
    lead_col = "forecast_period" if "forecast_period" in df.columns else "step"
    lead = df[lead_col]
    lead = lead if np.issubdtype(lead.dtype, np.timedelta64) else pd.to_timedelta(lead, unit="h")
    df["valid"] = pd.to_datetime(df[ref]) + lead
    df["lead_h"] = lead / pd.Timedelta(hours=1)
    # Erythemal irradiance never reaches 1 W/m²; values in the thousands are J/m² accumulated since the run began
    accumulated = df["v"].max() > 5
    if accumulated:
        df = df.sort_values([ref, "lead_h"])
        df["v"] = df.groupby(ref)["v"].diff() / 3600   # mean of the preceding hour, W/m²
    df = df[(df["lead_h"] >= 1) & (df["lead_h"] <= 24)]
    s = df.set_index("valid")["v"].clip(lower=0) * UV_PER_WM2
    return s, accumulated


def uv_series():
    """Hourly UV index at the station (UTC), or None while years are missing."""
    download_uv()
    years = range(UV_FIRST_YEAR, UV_LAST_YEAR + 1)
    missing = [y for y in years if not os.path.exists(uv_year_path(y))]
    if missing:
        print(f"CAMS UV: {missing} still missing; run again to retry")
        return None
    parts = [uv_file_series(uv_year_path(y)) for y in years]
    s = pd.concat(p[0] for p in parts).sort_index()
    s = s[~s.index.duplicated()]
    s = s.reindex(pd.date_range(s.index[0], s.index[-1], freq="h"))
    if parts[0][1]:
        # Hourly means of the preceding hour: average neighbours for the value at the hour (as for sunlight)
        s = (s + s.shift(-1)) / 2
    print(f"CAMS UV: {s.notna().sum()} hours, highest {s.max():.1f}, "
          f"{'accumulated' if parts[0][1] else 'instantaneous'} field")
    if not 5 <= s.max() <= 16:   # Thessaloniki's summer UV index peaks around 9-10
        print("CAMS UV: implausible values, so UV is left out; check the units in uv_file_series")
        return None
    return s.dropna()


def main():
    skip_uv = "--skip-uv" in sys.argv
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

    series = {v: (df[v], FIRST_YEAR, LAST_YEAR) for v in VARS}
    periods = {v: f"{FIRST_YEAR}-{LAST_YEAR}" for v in VARS}
    if not skip_uv:
        uv = uv_series()
        if uv is not None:
            series["uv"] = (uv, UV_FIRST_YEAR, UV_LAST_YEAR)
            periods["uv"] = f"{UV_FIRST_YEAR}-{UV_LAST_YEAR}"
            cells["cams_forecasts"] = {"area": UV_AREA, "interpolated_to": [LAT, LON]}

    hourly = {"doy": np.repeat(np.arange(1, 367), 24), "hour": np.tile(np.arange(24), 366)}
    daily = {"doy": np.arange(1, 367)}
    for v, (s, first, last) in series.items():
        hourly_stats, daily_stats = calendar_stats(s, first, last)
        for name, h, d in zip(("mean", "p10", "p90"), hourly_stats, daily_stats):
            hourly[f"{v}_{name}"] = h.reshape(-1).round(DECIMALS[v])
            daily[f"{v}_{name}"] = d.round(DECIMALS[v])
        print(f"{v}: done")

    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(hourly).to_csv(os.path.join(OUT_DIR, "kalamaria_hourly.csv"), index=False)
    pd.DataFrame(daily).to_csv(os.path.join(OUT_DIR, "kalamaria_daily.csv"), index=False)
    meta = {"periods": periods, "location": {"latitude": LAT, "longitude": LON},
            "window_days": WINDOW_DAYS, "lux_per_wm2": LUX_PER_WM2, "built": datetime.utcnow().strftime("%Y-%m-%d"),
            "sources": {"t": "era5_land", "rh": "era5_land", "p": "era5", "ws": "era5", "lux": "era5",
                        "uv": "CAMS global atmospheric composition forecasts, 00 UTC run, hours 1-24"},
            "grid_cells": cells, "units": {"t": "°C", "rh": "%", "p": "hPa (sea level)", "ws": "m/s (10 m)",
                                            "lux": "lux (station sensor scale)", "uv": "UV index"}}
    meta["sources"] = {k: v for k, v in meta["sources"].items() if k in series}
    with open(os.path.join(OUT_DIR, "kalamaria_meta.json"), "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    print("written to", OUT_DIR)


if __name__ == "__main__":
    sys.exit(main())
