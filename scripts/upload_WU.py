#!/usr/bin/env python3
"""
Upload the station's current conditions to Weather Underground every minute. Runs from ~ in its own
window ("WU Upload", started by ~/run_weather_station.sh).

The values come from ~/weather_station/station_now.py, shared with upload_awekas.py:
  temperature   newest MCP9808 reading
  rain          total since Athens midnight (WU's daily rain), from the gauge's running total
  wind          last 10 minutes: mean speed, strongest reading as the gust, vector-mean direction
  UV            newest reading
  PM2.5         not sent until the PMSA003I is cleaned (SEND_PM25)
  humidity, pressure  newest BME280 readings (station_now.INCLUDE_BME280), dew point from them
"""
import math
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, "/home/dimitris/weather_station")
import station_now  # noqa: E402

UPLOAD_INTERVAL_SECONDS = 60
SEND_PM25 = False  # the PMSA003I reads ~14x too high until it is cleaned; WU's parameter is AqPM2.5

# Weather Underground Config
WU_STATION_ID = "YOUR_STATION_ID"  # the live ~/upload_WU.py has the real value
WU_STATION_KEY = "YOUR_STATION_KEY"  # the live ~/upload_WU.py has the real value

WU_URL = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"


def calculate_dew_point(temp_c, humidity):
    """Dew point in °C (Magnus formula)."""
    try:
        b, c = 17.62, 243.12
        gamma = (b * temp_c / (c + temp_c)) + math.log(humidity / 100.0)
        return (c * gamma) / (b - gamma)
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def build_payload(c):
    """WU parameters (imperial units) for the current conditions; missing values are left out."""
    payload = {"ID": WU_STATION_ID, "PASSWORD": WU_STATION_KEY, "dateutc": "now", "action": "updateraw"}
    if c["temp_c"] is not None:
        payload["tempf"] = f"{c['temp_c'] * 9 / 5 + 32:.1f}"
    if c["humidity"] is not None:
        payload["humidity"] = f"{c['humidity']:.0f}"
        dew_point_c = calculate_dew_point(c["temp_c"], c["humidity"])
        if dew_point_c is not None:
            payload["dewptf"] = f"{dew_point_c * 9 / 5 + 32:.1f}"
    if c["pressure_hpa"] is not None:
        payload["baromin"] = f"{c['pressure_hpa'] * 0.0295300:.2f}"
    payload["dailyrainin"] = f"{c['rain_today_mm'] / 25.4:.2f}"
    if c["wind_kmh"] is not None:
        payload["windspeedmph"] = f"{c['wind_kmh'] / 1.609344:.1f}"
        payload["windgustmph"] = f"{c['gust_kmh'] / 1.609344:.1f}"
    if c["wind_dir"] is not None:
        payload["winddir"] = f"{c['wind_dir']:.0f}"
    if c["uv"] is not None:
        payload["UV"] = f"{c['uv']:.1f}"
    if SEND_PM25 and c["pm25"] is not None:
        payload["AqPM2.5"] = f"{c['pm25']:.1f}"
    return payload


def fmt(value, spec):
    return "-" if value is None else format(value, spec)


def upload_once():
    stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    c = station_now.current()
    if c is None:
        print(f"{stamp} skipped: no station data in the last {int(station_now.FRESH.total_seconds() // 60)} minutes")
        return
    summary = (f"T={fmt(c['temp_c'], '.1f')}°C rain={c['rain_today_mm']:.1f}mm wind={fmt(c['wind_kmh'], '.1f')}km/h "
               f"from {fmt(c['wind_dir'], '.0f')}° gust={fmt(c['gust_kmh'], '.1f')} UV={fmt(c['uv'], '.1f')}")
    try:
        r = requests.get(WU_URL, params=build_payload(c), timeout=30)
        reply = r.text.strip()[:200]
        result = "OK" if r.ok and reply.lower().startswith("success") else f"FAILED (HTTP {r.status_code}: {reply})"
    except requests.RequestException as e:
        result = f"FAILED ({type(e).__name__}: {e})"
    print(f"{stamp} {result} | {summary}", flush=True)


if __name__ == "__main__":
    print("--- Weather Underground uploader: one upload every minute ---", flush=True)
    while True:
        try:
            upload_once()
        except Exception as e:  # a bad cycle must not end the loop
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR: {type(e).__name__}: {e}", flush=True)
        time.sleep(UPLOAD_INTERVAL_SECONDS)
