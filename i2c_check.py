#!/usr/bin/env python3
"""
Health check of the I2C sensors (rain gauge SEN0575 at 0x1D, lightning AS3935 at 0x03, PMSA003I at 0x12)
and the scripts that read them. Read-only: it only reads registers, never writes.

  python3 i2c_check.py [since "YYYY-mm-dd HH:MM" local time]

`since` is when things were last known good (default: 2026-11-01 16:11, when the AS3935 was reconnected);
a script started after it, or a gauge powered up after it, means something restarted.
"""
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import smbus

CSV_PATH = "/home/dimitris/weather_station/weather_station_data.csv"
SINCE_DEFAULT = "2026-11-01 16:11"
TEST_SECONDS = 60
DEVICES = [("rain gauge", 0x1D, 0x18, 2), ("AS3935", 0x03, 0x00, 1), ("PMSA003I", 0x12, 0x00, 2)]
# AS3935: register 0x00 only; reading 0x03 would clear a pending lightning interrupt


def process_start(pattern):
    """(pid, start time) of processes whose command line is exactly `pattern`."""
    out = subprocess.run(["ps", "-eo", "pid=,lstart=,args="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) == 7 and parts[6] == pattern:
            found.append((int(parts[0]), datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")))
    return found


def main():
    since = datetime.strptime(sys.argv[1] if len(sys.argv) > 1 else SINCE_DEFAULT, "%Y-%m-%d %H:%M")
    now = datetime.now()
    print(f"I2C sensor check at {now:%Y-%m-%d %H:%M} (local), compared with {since:%Y-%m-%d %H:%M}")
    problems = []

    print("\nScripts:")
    for label, cmd in (("lightning", "python3 lightning_detector2.py"), ("lightning wrapper", "bash auto_lightning.sh"),
                       ("rain", "python rainfall2.py"), ("air quality", "python air_quality.py")):
        procs = process_start(cmd)
        if not procs:
            print(f"  {label:17s} not running")
            if label in ("lightning", "rain"):
                problems.append(f"{label} script not running")
        for pid, start in procs:
            restarted = start > since + timedelta(minutes=2)  # margin: `since` has minute resolution
            print(f"  {label:17s} PID {pid}, running since {start:%Y-%m-%d %H:%M:%S}{'  <- restarted after ' + f'{since:%H:%M}' if restarted else ''}")
            if restarted and label == "lightning":
                problems.append("the lightning script restarted (it exits when the AS3935 stops answering)")

    bus = smbus.SMBus(1)
    print("\nRain gauge uptime:")
    try:
        st = bus.read_i2c_block_data(0x1D, 0x18, 2)
        minutes = st[0] | st[1] << 8
        powered = now - timedelta(minutes=minutes)
        print(f"  working time {minutes} min = powered up around {powered:%Y-%m-%d %H:%M}"
              f"{'  <- it restarted after ' + f'{since:%H:%M}' if powered > since + timedelta(minutes=2) else ''}")
        if powered > since + timedelta(minutes=2):
            problems.append("the rain gauge restarted (bus or power disturbance)")
    except OSError as e:
        print(f"  could not read: {e}")
        problems.append("rain gauge not answering")

    print(f"\nBus test, {TEST_SECONDS} s (2 reads per second per device):")
    present = []
    for name, addr, reg, n in DEVICES:
        try:
            bus.read_i2c_block_data(addr, reg, n)
            present.append((name, addr, reg, n))
        except OSError:
            time.sleep(0.2)
            try:
                bus.read_i2c_block_data(addr, reg, n)
                present.append((name, addr, reg, n))
            except OSError:
                print(f"  {name} ({addr:#04x}): not answering (unplugged?)")
    counts = {addr: [0, 0] for _, addr, _, _ in present}
    t0 = time.time()
    while time.time() - t0 < TEST_SECONDS:
        for name, addr, reg, n in present:
            try:
                bus.read_i2c_block_data(addr, reg, n)
                counts[addr][0] += 1
            except OSError:
                counts[addr][1] += 1
        time.sleep(0.5)
    for name, addr, _, _ in present:
        ok, bad = counts[addr]
        print(f"  {name} ({addr:#04x}): {ok} good, {bad} failed")
        if bad:
            problems.append(f"{bad} failed reads from the {name}")

    print("\nCSV rows since then:")
    since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lightning, rain = [], []
    with open(CSV_PATH, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 1_500_000))
        for raw in f.read().decode("utf-8", "ignore").splitlines():
            row = raw.split(",")
            if len(row) > 10 and row[0][:16] >= since_utc:
                if row[5].strip() or row[6].strip():
                    lightning.append(raw[:60])
                if row[10].strip():
                    rain.append(f"{row[0]} {row[10]} mm")
    print(f"  lightning/disturber rows: {len(lightning)}" + "".join(f"\n    {r}" for r in lightning[-5:]))
    print(f"  rain rows (running total since UTC midnight): {len(rain)}" + "".join(f"\n    {r}" for r in rain[-5:]))

    print("\nVerdict:", "all good, no interruption" if not problems else "PROBLEMS: " + "; ".join(problems))


if __name__ == "__main__":
    main()
