#!/usr/bin/env python3
"""
Monthly archive of weather_station_data.csv. Cron runs it every night at 03:17; most nights it has nothing to do.

- Each finished month (by the rows' UTC timestamps) is copied to raw_data_months/<Month>_<YYYY>.csv
  (e.g. October_2026.csv), with the CSV's header, as soon as the month is over. Rows that turn up later for a
  month already archived are appended to its file.
- KEEP_DAYS after a month has ended, its rows are removed from the live CSV. So the live file holds the
  current month, plus the previous one during the first week: readers look back up to a few days
  (station_now 2 days, the notifier ~4 days, the dashboard 24 h), which a removal on the 1st would cut short.
- Rows dated before 2020 (an ESP32 board whose clock wasn't set) are moved to raw_data_months/Unset_clock.csv.
- Lines without a readable timestamp, and rows of the current or a future month, stay in the live CSV.
- Nothing is removed from the live CSV unless every removed row has been found in its archive file.

Writers append to the live CSV all the time, each opening it, appending one row and closing it. So the
trimmed file is swapped in atomically, and rows appended to the old file around the swap are copied over.

  python3 archive_months.py            archive and trim
  python3 archive_months.py --dry-run  only report what would be done
"""
import argparse
import fcntl
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = "/home/dimitris/weather_station"
LIVE_CSV = os.path.join(BASE_DIR, "weather_station_data.csv")
ARCHIVE_DIR = os.path.join(BASE_DIR, "raw_data_months")
LOCK_FILE = os.path.join(BASE_DIR, ".archive_months.lock")
UNSET_CLOCK_FILE = "Unset_clock.csv"
KEEP_DAYS = 7      # a finished month stays in the live CSV this long after it ends
SETTLE_S = 5       # after the swap, wait this long for writers that had opened the old file
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December"]


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} archive_months: {message}", flush=True)


def month_key(line):
    """'YYYY-MM' for a row, 'unset' for a row dated before 2020, None when there is no readable timestamp."""
    if len(line) < 20 or line[19:20] != b",":
        return None
    try:
        t = datetime.fromisoformat(line[:19].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return "unset" if t.year < 2020 else f"{t.year:04d}-{t.month:02d}"


def archive_name(key):
    if key == "unset":
        return UNSET_CLOCK_FILE
    year, month = map(int, key.split("-"))
    return f"{MONTH_NAMES[month - 1]}_{year}.csv"


def month_end(key):
    year, month = map(int, key.split("-"))
    return datetime(year + month // 12, month % 12 + 1, 1, tzinfo=timezone.utc)


def read_archive(path):
    """(header, set of row lines) of an existing archive file."""
    with open(path, "rb") as f:
        header, _, body = f.read().partition(b"\n")
    return header.rstrip(b"\r"), set(body.split(b"\n")) - {b""}


def write_archive(path, header, rows, dry_run):
    """Creates the archive, or appends the rows it doesn't have yet. Returns how many rows were added."""
    if os.path.exists(path):
        old_header, existing = read_archive(path)
        if old_header != header:
            raise ValueError(f"{os.path.basename(path)} has a different header; not touching it")
        new = [r for r in rows if r not in existing]
        if new and not dry_run:
            with open(path, "ab") as f:
                f.write(b"".join(r + b"\n" for r in new))
                f.flush()
                os.fsync(f.fileno())
        return len(new)
    if not dry_run:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(header + b"\n" + b"".join(r + b"\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Archive finished months of the live CSV and trim it.")
    ap.add_argument("--dry-run", action="store_true", help="only report what would be done")
    args = ap.parse_args()

    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run is in progress; exiting")
        return 1

    now = datetime.now(timezone.utc)
    current = f"{now.year:04d}-{now.month:02d}"
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    with open(LIVE_CSV, "rb") as live:
        data = live.read()
        # Only complete lines; anything after the last newline is picked up with the late rows
        read_upto = data.rfind(b"\n") + 1
        header, _, body = data[:read_upto].partition(b"\n")
        header = header.rstrip(b"\r")
        if not header.startswith(b"timestamp,"):
            log("the live CSV's first line isn't the header; exiting without changes")
            return 1

        groups = {}
        for line in body.split(b"\n"):
            key = month_key(line)
            if key is not None and (key == "unset" or key < current):
                groups.setdefault(key, []).append(line)
        if not groups:
            log("nothing to archive")
            return 0

        # 1. Archive every finished month (and the undated rows)
        to_trim = set()
        for key in sorted(groups):
            path = os.path.join(ARCHIVE_DIR, archive_name(key))
            rows = groups[key]
            try:
                added = write_archive(path, header, rows, args.dry_run)
            except (OSError, ValueError) as e:
                log(f"{archive_name(key)}: {e}")
                continue
            due = key == "unset" or now >= month_end(key) + timedelta(days=KEEP_DAYS)
            log(f"{archive_name(key)}: {len(rows):,} rows in the live CSV, {added:,} "
                f"{'would be ' if args.dry_run else ''}added to the archive; "
                f"{'removing them from the live CSV' if due else 'kept in the live CSV until ' + (month_end(key) + timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')}")
            if due:
                to_trim.add(key)

        if args.dry_run or not to_trim:
            return 0

        # 2. Check that every row about to leave the live CSV is in its archive
        for key in sorted(to_trim):
            path = os.path.join(ARCHIVE_DIR, archive_name(key))
            _, archived = read_archive(path)
            missing = [r for r in groups[key] if r not in archived]
            if missing:
                log(f"{archive_name(key)}: {len(missing)} rows not found in the archive; keeping them in the live CSV")
                to_trim.discard(key)
        if not to_trim:
            return 1

        # 3. Write the trimmed live CSV and swap it in
        kept = [line for line in body.split(b"\n") if month_key(line) not in to_trim]
        tmp = LIVE_CSV + ".tmp"
        with open(tmp, "wb") as f:
            f.write(header + b"\n" + b"".join(line + b"\n" for line in kept if line))
            live.seek(read_upto)
            appended = live.read()                        # rows written since the first read
            appended = appended[:appended.rfind(b"\n") + 1]
            f.write(appended)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(LIVE_CSV, tmp)
        pos = read_upto + len(appended)
        os.replace(tmp, LIVE_CSV)

        # 4. Rows appended to the old file around the swap (by writers that had opened it) go to the new one
        late_total = 0
        for _ in range(2):
            time.sleep(SETTLE_S)
            live.seek(pos)
            late = live.read()
            late = late[:late.rfind(b"\n") + 1]
            if late:
                with open(LIVE_CSV, "ab") as f:
                    f.write(late)
                pos += len(late)
                late_total += late.count(b"\n")

    removed = sum(len(groups[k]) for k in to_trim)
    log(f"live CSV trimmed: {removed:,} rows moved to the archives, {late_total} rows written during the swap "
        f"copied over; it now starts with {min((k for k in groups if k not in to_trim), default=current)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
