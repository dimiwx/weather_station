#!/usr/bin/env python3
"""
Copies the live CSV's rows into the SQLite DB (/home/dimitris/weather_station.db, table station_readings).
Cron runs it every 30 minutes.

Since 2026-11-22 it compares rows, not just times: every row of the last LOOKBACK in the CSV that the DB doesn't
have yet is added. The previous version only added rows newer than the newest one already in the DB, so rows
that arrived late, or carried a board clock's wrong time, were skipped for good (about 11,000 rows on
2026-10-25 to 28, when the boards' clocks missed the end of daylight-saving time).
Rows dated before 2020 (a board whose clock wasn't set) are skipped; rows dated in the future are added on a
later run, once their time has come.
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta

CSV_PATH = "/home/dimitris/weather_station/weather_station_data.csv"
DB_PATH = "/home/dimitris/weather_station.db"
TABLE = "station_readings"
LOOKBACK = timedelta(days=3)
TAIL_BYTES = 2_000_000          # ~2 weeks of CSV, enough to cover LOOKBACK
TEXT_COLS = {5, 7}              # Lightning Detection, Rain Event


def value(col, s):
    s = s.strip()
    if s == "":
        return None
    if col in TEXT_COLS:
        return s
    try:
        return float(s)
    except ValueError:
        return s


def key(row):
    """A row as compared with the DB: numbers rounded, so 17.5 and 17.50 count as the same."""
    return tuple(round(v, 6) if isinstance(v, float) else v for v in row)


def main():
    now = datetime.utcnow()
    since = (now - LOOKBACK).strftime("%Y-%m-%d %H:%M:%S")
    latest_allowed = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    with open(CSV_PATH, "rb") as f:
        header = f.readline().decode("utf-8", "replace").rstrip("\r\n").split(",")
        size = os.path.getsize(CSV_PATH)
        f.seek(max(f.tell(), size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").split("\n")[1:-1]   # first may be cut, last may be unfinished
    ncols = len(header)

    rows = {}
    for line in lines:
        p = line.rstrip("\r").split(",")
        ts = p[0][:19]
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t.year < 2020 or ts < since or ts > latest_allowed:
            continue
        p += [""] * (ncols - len(p))
        row = (ts + ".000000",) + tuple(value(c, p[c]) for c in range(1, ncols))
        if any(v is not None for v in row[1:]):
            rows[key(row)] = row

    con = sqlite3.connect(DB_PATH, timeout=60)
    have = set(key(r) for r in con.execute(f"SELECT * FROM {TABLE} WHERE timestamp >= ?", (since + ".000000",)))
    new = [r for k, r in rows.items() if k not in have]
    if new:
        marks = ",".join("?" * ncols)
        con.executemany(f"INSERT INTO {TABLE} VALUES ({marks})", sorted(new, key=lambda r: r[0]))
        con.commit()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} upload_database: {len(rows)} CSV rows in the last "
          f"{LOOKBACK.days} days, {len(new)} new ones added to the DB")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} upload_database: ERROR {type(e).__name__}: {e}")
        sys.exit(1)
