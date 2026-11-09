#!/usr/bin/env python3
"""
Upload the station's current conditions to AWEKAS (awekas.at). Run by cron every 5 minutes.

One run = one upload, using the AWEKAS API (data.awekas.at/eingabe_pruefung.php): a GET request whose
`val` is a semicolon-separated list where each position has a fixed meaning; missing values stay empty.
Credentials come from .env: AWEKAS_USERNAME and AWEKAS_PASSWORD (sent as an MD5 hash).

The values come from station_now.py (shared with ~/upload_WU.py): the MCP9808 temperature, rain since
Athens midnight, 10-minute wind (mean, strongest reading as the gust, vector-mean direction), UV, light,
and the BME280's humidity and pressure (station_now.INCLUDE_BME280). Not sent: PM (the PMSA003I needs cleaning).

  python3 upload_awekas.py            upload, and append one line to awekas.log
  python3 upload_awekas.py --dry-run  print what would be sent, without uploading
"""
import hashlib
import json
import os
import sys
import urllib.parse
from datetime import datetime

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import station_now  # noqa: E402

BASE_DIR = "/home/dimitris/weather_station"
LOG_PATH = os.path.join(BASE_DIR, "awekas.log")
LOG_MAX_BYTES = 300_000          # the log is trimmed to its newest half beyond this
URL = "https://data.awekas.at/eingabe_pruefung.php"
SOFTWARE_FLAG = "KriniWS_1.0"
LOCAL_TZ = station_now.LOCAL_TZ


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


def fmt(value, spec):
    return "" if value is None else format(value, spec)


def main():
    dry_run = "--dry-run" in sys.argv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    user, password = os.getenv("AWEKAS_USERNAME"), os.getenv("AWEKAS_PASSWORD")
    stamp = f"{datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}"
    if not user or not password:
        log(f"{stamp} ERROR: AWEKAS_USERNAME / AWEKAS_PASSWORD missing from .env")
        return 1

    c = station_now.current()
    if c is None:
        log(f"{stamp} skipped: no station data in the last {int(station_now.FRESH.total_seconds() // 60)} minutes")
        return 0

    local = c["time"].astimezone(LOCAL_TZ)
    fields = [
        user,                                      # 1 username
        hashlib.md5(password.encode("utf-8")).hexdigest(),  # 2 password, MD5
        f"{local:%d.%m.%Y}", f"{local:%H:%M}",     # 3-4 date and time, local
        fmt(c["temp_c"], ".1f"),                   # 5 temperature, °C
        fmt(c["humidity"], ".0f"),                 # 6 humidity, %
        fmt(c["pressure_hpa"], ".1f"),             # 7 pressure, hPa (sea level)
        f"{c['rain_today_mm']:.1f}",               # 8 rain since local midnight, mm
        fmt(c["wind_kmh"], ".1f"),                 # 9 mean wind, km/h
        fmt(c["wind_dir"], ".0f"),                 # 10 wind direction, °
        "", "", "",                                # 11-13 weather code, warning text, snow depth
        "en",                                      # 14 language
        "",                                        # 15 pressure tendency
        fmt(c["gust_kmh"], ".1f"),                 # 16 gust, km/h
        "",                                        # 17 solar radiation, W/m²
        fmt(c["uv"], ".1f"),                       # 18 UV index
        fmt(c["lux"], ".0f"),                      # 19 brightness, lux
        "", "", "",                                # 20-22 sunshine hours, soil temperature, rain rate
        SOFTWARE_FLAG,                             # 23 software flag
    ]
    val = ";".join(fields)
    summary = (f"T={fmt(c['temp_c'], '.1f') or '-'}°C rain={c['rain_today_mm']:.1f}mm "
               f"wind={fmt(c['wind_kmh'], '.1f') or '-'}km/h from {fmt(c['wind_dir'], '.0f') or '-'}° "
               f"gust={fmt(c['gust_kmh'], '.1f') or '-'} UV={fmt(c['uv'], '.1f') or '-'} lux={fmt(c['lux'], '.0f') or '-'}")
    if dry_run:
        print("Would send:", val.replace(fields[1], "<md5 of password>"))
        print(summary)
        return 0

    try:
        r = requests.get(URL + "?val=" + urllib.parse.quote(val, safe=";.:_-") + "&output=json", timeout=30)
        body = r.text.strip()
    except requests.RequestException as e:
        log(f"{stamp} ERROR: {type(e).__name__}: {e} | {summary}")
        return 1
    try:
        reply = json.loads(body)
        status = reply.get("status")
        result = {2: "OK", 1: "partly stored"}.get(status, "REJECTED")
        # Report only what is wrong: fields AWEKAS flagged as errors or disabled, a failed login, a rate limit
        problems = []
        errors = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        disabled = reply.get("disabled") if isinstance(reply.get("disabled"), dict) else {}
        if any(v for k, v in errors.items() if k != "count"):
            problems.append("errors in: " + ", ".join(k for k, v in errors.items() if v and k != "count"))
        if any(disabled.values()):
            problems.append("disabled on AWEKAS: " + ", ".join(k for k, v in disabled.items() if v))
        if reply.get("authentication") == 0:
            problems.append("login failed (check AWEKAS_USERNAME / AWEKAS_PASSWORD)")
        if status == 0 and reply.get("minuploadtime") == 0:
            problems.append("rate limited")
        detail = "; ".join(problems)
    except ValueError:
        result = "OK" if body.startswith("OK") else "REJECTED"
        detail = body[:200]
    log(f"{stamp} {result} (HTTP {r.status_code}){': ' + detail if detail else ''} | {summary}")
    return 0 if result == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
