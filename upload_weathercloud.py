#!/usr/bin/env python3
"""
Upload the station's current conditions to Weathercloud (weathercloud.net). Run by cron every 10 minutes:
a free Weathercloud account accepts one upload per 10 minutes and answers 429 to anything faster.

One run = one upload: a GET to api.weathercloud.net/v01/set (the protocol Cumulus MX and pywws use).
Temperatures, wind speeds, pressure, rain and UV are sent as integers in tenths (21.4 °C -> 214),
humidity and directions as whole numbers, the date in UTC. Missing values are left out.
Weathercloud answers with an HTTP status: 200 stored, 400 bad request, 401 wrong ID or key,
429 too many requests, 500 server error. Credentials come from .env: WEATHERCLOUD_ID and WEATHERCLOUD_KEY,
the "Weathercloud ID" and "Key" on the device's settings page at weathercloud.net.

The values come from station_now.py (shared with upload_awekas.py and ~/upload_WU.py): the MCP9808
temperature, the BME280's humidity and pressure (station_now.INCLUDE_BME280), rain since Athens midnight,
10-minute wind (mean, strongest reading as the gust, vector-mean direction) and UV. Derived here: dew point
(Magnus, from the MCP9808 temperature and BME280 humidity, as ~/upload_WU.py does), wind chill and heat index.
Not sent: PM (the PMSA003I needs cleaning), and solar radiation unless SEND_SOLAR_ESTIMATE (the station has
no pyranometer; the estimate divides the VEML7700's lux by LUX_PER_WM2).

  python3 upload_weathercloud.py            upload, and append one line to weathercloud.log
  python3 upload_weathercloud.py --dry-run  print what would be sent, without uploading
"""
import math
import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import station_now  # noqa: E402

BASE_DIR = "/home/dimitris/weather_station"
LOG_PATH = os.path.join(BASE_DIR, "weathercloud.log")
LOG_MAX_BYTES = 300_000          # the log is trimmed to its newest half beyond this
URL = "https://api.weathercloud.net/v01/set"
SOFTWARE = "KriniWS_1.0"
LOCAL_TZ = station_now.LOCAL_TZ
SEND_SOLAR_ESTIMATE = False      # solar radiation estimated from the light sensor (see LUX_PER_WM2)
LUX_PER_WM2 = 60.0               # the VEML7700 reads ~55-60 lux per W/m² of model shortwave radiation
RESPONSES = {
    "200": "OK",
    "400": "bad request",
    "401": "wrong WEATHERCLOUD_ID or WEATHERCLOUD_KEY",
    "429": "too many requests (a free account accepts one upload per 10 minutes)",
    "500": "Weathercloud server error",
}


def log(line):
    print(line)
    if "--dry-run" in sys.argv:
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines[len(lines) // 2:])
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"Could not write {LOG_PATH}: {e}")


def dew_point(temp_c, humidity):
    """Dew point in °C (Magnus formula), or None."""
    if temp_c is None or not humidity or humidity <= 0:
        return None
    b, c = 17.62, 243.12
    gamma = b * temp_c / (c + temp_c) + math.log(min(humidity, 100.0) / 100.0)
    return c * gamma / (b - gamma)


def wind_chill(temp_c, wind_kmh):
    """Wind chill in °C (Environment Canada / NWS 2001). Equals the temperature outside the formula's
    range (above 10 °C, or wind of 4.8 km/h or less)."""
    if temp_c is None:
        return None
    if wind_kmh is None or temp_c > 10 or wind_kmh <= 4.8:
        return temp_c
    v = wind_kmh ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v


def heat_index(temp_c, humidity):
    """Heat index in °C (NWS: Steadman's simple formula, Rothfusz's regression from 80 °F).
    Equals the temperature below 26.7 °C or without humidity."""
    if temp_c is None:
        return None
    if humidity is None or temp_c < 26.7:
        return temp_c
    t, rh = temp_c * 9 / 5 + 32, humidity
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)
    if (hi + t) / 2 >= 80:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh - 6.83783e-3 * t * t
              - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh)
        if rh < 13 and 80 <= t <= 112:
            hi -= (13 - rh) / 4 * math.sqrt((17 - abs(t - 95)) / 17)
        elif rh > 85 and 80 <= t <= 87:
            hi += (rh - 85) / 10 * (87 - t) / 5
    return max((hi - 32) * 5 / 9, temp_c)


def tenths(value):
    return None if value is None else str(int(round(value * 10)))


def whole(value):
    return None if value is None else str(int(round(value)))


def main():
    dry_run = "--dry-run" in sys.argv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    wid, key = os.getenv("WEATHERCLOUD_ID"), os.getenv("WEATHERCLOUD_KEY")
    stamp = f"{datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}"
    if (not wid or not key) and not dry_run:
        log(f"{stamp} ERROR: WEATHERCLOUD_ID / WEATHERCLOUD_KEY missing from .env")
        return 1

    c = station_now.current()
    if c is None:
        log(f"{stamp} skipped: no station data in the last {int(station_now.FRESH.total_seconds() // 60)} minutes")
        return 0

    t, rh = c["temp_c"], c["humidity"]
    wind_ms = None if c["wind_kmh"] is None else c["wind_kmh"] / 3.6
    gust_ms = None if c["gust_kmh"] is None else c["gust_kmh"] / 3.6
    solar = c["lux"] / LUX_PER_WM2 if SEND_SOLAR_ESTIMATE and c["lux"] is not None else None
    values = {
        "temp": tenths(t),                              # °C × 10
        "hum": whole(rh),                               # %
        "dew": tenths(dew_point(t, rh)),                # °C × 10
        "chill": tenths(wind_chill(t, c["wind_kmh"])),  # °C × 10
        "heat": tenths(heat_index(t, rh)),              # °C × 10
        "bar": tenths(c["pressure_hpa"]),               # hPa × 10 (sent as sea level, as to AWEKAS and WU)
        "wspd": tenths(wind_ms),                        # m/s × 10: the 10-minute mean
        "wspdavg": tenths(wind_ms),                     # m/s × 10
        "wspdhi": tenths(gust_ms),                      # m/s × 10: strongest reading in 10 minutes
        "wdir": whole(c["wind_dir"]),                   # degrees, vector mean
        "wdiravg": whole(c["wind_dir"]),
        "rain": tenths(c["rain_today_mm"]),             # mm × 10 since local midnight
        "uvi": tenths(c["uv"]),                         # UV index × 10
        "solarrad": tenths(solar),                      # W/m² × 10
    }
    params = {k: v for k, v in values.items() if v is not None}
    params["date"] = f"{c['time']:%Y%m%d}"              # UTC
    params["software"] = SOFTWARE
    summary = " ".join(f"{k}={v}" for k, v in params.items() if k not in ("date", "software"))
    if dry_run:
        print("Would send:", URL + "?wid=<id>&key=<key>&" + "&".join(f"{k}={v}" for k, v in params.items()))
        print(f"T={t}°C RH={rh}% dew={dew_point(t, rh)} wind={c['wind_kmh']} km/h gust={c['gust_kmh']} "
              f"from {c['wind_dir']}° rain={c['rain_today_mm']} mm UV={c['uv']}")
        return 0

    try:
        r = requests.get(URL, params={"wid": wid, "key": key, **params}, timeout=30)
    except requests.RequestException as e:
        # The exception text can contain the URL, with the key in it
        log(f"{stamp} ERROR: {type(e).__name__}: {str(e).replace(key, '<key>')} | {summary}")
        return 1
    body = r.text.strip()
    # The status is the HTTP code; older versions of the API also put it in the body
    code = body if body.isdigit() and len(body) == 3 else str(r.status_code)
    result = "OK" if code == "200" else "REJECTED"
    detail = "" if code == "200" else f": {RESPONSES.get(code, 'unknown answer')} ({code}) {body[:120]}"
    log(f"{stamp} {result} (HTTP {r.status_code}){detail} | {summary}")
    return 0 if result == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
