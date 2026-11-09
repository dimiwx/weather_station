"""
Current conditions from weather_station_data.csv, shared by the uploaders: upload_awekas.py (here, cron)
and ~/upload_WU.py (its own window).

Writers append sparse rows (the timestamp plus their own columns), not in time order. So each value is
the newest reading of its own column, wind uses a 10-minute window, and anything older than FRESH counts
as missing.
"""
import csv
import io
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CSV_PATH = "/home/dimitris/weather_station/weather_station_data.csv"
LOCAL_TZ = ZoneInfo("Europe/Athens")
TAIL_BYTES = 1_200_000           # ~6 days of CSV at the current ~185 KB/day; only LOOKBACK of it is parsed
LOOKBACK = timedelta(days=2)     # rain since Athens midnight needs the gauge readings from before it
FRESH = timedelta(minutes=15)    # older readings count as missing
WIND_WINDOW = timedelta(minutes=10)
INCLUDE_BME280 = True            # BME280 humidity and pressure (new sensor, data since 2026-11-09)

# CSV columns (0 = timestamp, UTC)
COL_HUMIDITY, COL_PRESSURE, COL_LIGHT = 2, 3, 4
COL_TEMP_MCP, COL_UV, COL_RAIN, COL_WIND_SPEED, COL_WIND_DIR = 8, 9, 10, 11, 12
COL_PM25 = 14


def read_rows(now):
    """(time UTC, row) for the last LOOKBACK, sorted by time. Rows dated in the future (NTP glitches) are dropped."""
    cutoff = (now - LOOKBACK).strftime("%Y-%m-%d %H:%M:%S")
    with open(CSV_PATH, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        data = f.read().replace(b"\x00", b"")
    # The timestamp format sorts as text, so old lines can be dropped before the (slower) CSV parsing
    recent = b"\n".join(line for line in data.split(b"\n") if line[:19] >= cutoff.encode())
    rows = []
    for row in csv.reader(io.StringIO(recent.decode("utf-8", "ignore"))):
        if not row:
            continue
        try:  # fromisoformat: strptime is ~20x slower, which on the Pi costs most of a second per call
            t = datetime.fromisoformat(row[0][:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # the partial first line, the header, or a damaged line
        if t <= now + timedelta(minutes=1):
            rows.append((t, row))
    rows.sort(key=lambda r: r[0])
    return rows


def readings(rows, col, since):
    """(time, value) of column `col` from `since` on."""
    out = []
    for t, row in rows:
        if t >= since and len(row) > col and row[col].strip():
            try:
                out.append((t, float(row[col])))
            except ValueError:
                pass
    return out


def newest(rows, col, now):
    values = readings(rows, col, now - FRESH)
    return values[-1][1] if values else None


def rain_since_local_midnight(rows, now):
    """The gauge logs its running total since UTC midnight each time the bucket tips, and the total also
    restarts from 0 when rainfall2.py restarts. Add up the increases since Athens midnight; a drop, or a new
    UTC day, means the total restarted, so the new value is all new rain."""
    midnight = now.astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    values = readings(rows, COL_RAIN, midnight - timedelta(days=1))
    total = 0.0
    prev_t = prev_v = None
    for t, v in values:  # t is UTC, so t.date() is the gauge's day
        if t >= midnight:
            restarted = prev_v is None or t.date() != prev_t.date() or v < prev_v
            total += v if restarted else v - prev_v
        prev_t, prev_v = t, v
    return total


def wind(rows, now):
    """Mean speed and gust (km/h) and vector-mean direction (whole degrees) over the last WIND_WINDOW."""
    samples = []
    for t, row in rows:
        if t >= now - WIND_WINDOW and len(row) > COL_WIND_DIR and row[COL_WIND_SPEED].strip():
            try:
                speed = float(row[COL_WIND_SPEED])
                direction = float(row[COL_WIND_DIR]) if row[COL_WIND_DIR].strip() else None
            except ValueError:
                continue
            samples.append((speed, direction))
    if not samples:
        return None, None, None
    mean_kmh = sum(s for s, _ in samples) / len(samples) * 3.6
    gust_kmh = max(s for s, _ in samples) * 3.6
    # Direction only means something while the wind blows; average it as vectors, never arithmetically
    dirs = [d for s, d in samples if d is not None and s > 0]
    direction = None
    if dirs:
        u = sum(math.sin(math.radians(d)) for d in dirs)
        v = sum(math.cos(math.radians(d)) for d in dirs)
        direction = round(math.degrees(math.atan2(u, v))) % 360
    return mean_kmh, gust_kmh, direction


def current(now=None):
    """The station's current conditions as a dict (None for each missing value),
    or None when the station has written nothing for FRESH."""
    now = now or datetime.now(timezone.utc)
    rows = read_rows(now)
    if not rows or now - rows[-1][0] > FRESH:
        return None
    wind_kmh, gust_kmh, wind_dir = wind(rows, now)
    return {
        "time": now,
        "temp_c": newest(rows, COL_TEMP_MCP, now),   # the MCP9808: the BME280 died on 2026-10-11
        "humidity": newest(rows, COL_HUMIDITY, now) if INCLUDE_BME280 else None,
        "pressure_hpa": newest(rows, COL_PRESSURE, now) if INCLUDE_BME280 else None,
        "rain_today_mm": rain_since_local_midnight(rows, now),
        "wind_kmh": wind_kmh,
        "gust_kmh": gust_kmh,
        "wind_dir": wind_dir,
        "uv": newest(rows, COL_UV, now),
        "lux": newest(rows, COL_LIGHT, now),
        "pm25": newest(rows, COL_PM25, now),
    }
