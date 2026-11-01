#!/usr/bin/env python3
"""
Log rain from the DFRobot SEN0575 tipping-bucket gauge (I2C address 0x1D) to the station CSV.

Each time the bucket tips, a row is appended with the running total since UTC midnight in the
"Rain Accumulation (SEN0575) (mm)" column (the 11th). Readers (dashboard, station_now.py) rely on that.

Robustness (rewritten 2026-11-01):
- The vendor library returns zeros when an I2C read fails, which looked like real readings (a bucket
  count of 0) and produced dozens of fake "0.0 mm" rows a day. Here a failed read raises and the
  minute is skipped.
- A read is only accepted when cumulative mm = tips x mm-per-tip, which rejects corrupted bytes.
- The day's total is built from the increases between good reads. If the gauge restarts (its counts
  go back to 0), the new count is added on top instead of making the total go negative.
- On start the day's total resumes from the CSV, so restarting this script doesn't zero the chart.

Until 2026-11-01 the gauge kept restarting because the PMSA003I air-quality sensor disturbed the
shared I2C bus (dropouts of ~0.6 s every 1.7 s), so no rain was recorded from mid-August on.
"""
import csv
import datetime
import time

from DFRobot_RainfallSensor import DFRobot_RainfallSensor_I2C

CSV_FILE_PATH = "/home/dimitris/weather_station/weather_station_data.csv"
RAIN_COLUMN = 10            # 0-based index of "Rain Accumulation (SEN0575) (mm)"
POLL_SECONDS = 60
MAX_MM_PER_POLL = 25.0      # more than this in one minute can only be a bad read


class RainfallSensor(DFRobot_RainfallSensor_I2C):
    """The vendor class, except that a failed I2C read raises OSError instead of returning zeros."""

    def _read_register(self, reg, length):
        return self.i2c.read_i2c_block_data(self._addr, reg, length)

    def mm_per_tip(self):
        data = self._read_register(self.SEN0575_I2C_REG_RAW_BASE_RAINFALL, 2)
        return (data[0] | data[1] << 8) / 10000.0


def utc_now():
    return datetime.datetime.utcnow()


def read_gauge(sensor, per_tip):
    """(tips, cumulative mm) from one consistent pair of reads; raises OSError or ValueError on a bad read."""
    tips = sensor.get_raw_data()
    cumulative = sensor.get_rainfall()
    if abs(cumulative - tips * per_tip) > per_tip / 2:
        raise ValueError(f"inconsistent read: {tips} tips but {cumulative} mm")
    return tips, cumulative


def todays_total_from_csv(today):
    """The newest rain total already logged for this UTC day, so a restart continues from it."""
    prefix = today.strftime("%Y-%m-%d")
    total = 0.0
    try:
        with open(CSV_FILE_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lines = f.read().replace(b"\x00", b"").decode("utf-8", "ignore").splitlines()
        newest = ""
        for row in csv.reader(lines):
            if len(row) > RAIN_COLUMN and row[0].startswith(prefix) and row[RAIN_COLUMN].strip() and row[0] >= newest:
                try:
                    total, newest = float(row[RAIN_COLUMN]), row[0]
                except ValueError:
                    pass
    except OSError as e:
        print(f"Could not read today's total from the CSV: {e}")
    return total


def log_to_csv(timestamp, daily_total):
    """Append [timestamp, 9 empty columns, daily total]; the other writers fill the other columns."""
    row = [timestamp] + [""] * (RAIN_COLUMN - 1) + [f"{daily_total:.4f}"]
    try:
        with open(CSV_FILE_PATH, "a", newline="") as f:
            csv.writer(f).writerow(row)
        print("Logged:", row)
    except OSError as e:
        print("Error writing to CSV file:", e)


def setup():
    sensor = RainfallSensor()
    while True:
        try:
            if sensor.begin():
                per_tip = sensor.mm_per_tip()
                if per_tip > 0:
                    break
                print(f"Gauge reports {per_tip} mm per tip; retrying")
            else:
                print("Gauge did not identify itself (wrong PID/VID); retrying")
        except OSError as e:
            print(f"Gauge not answering on I2C ({e}); retrying")
        time.sleep(5)
    print(f"Gauge found: firmware {sensor.get_firmware_version()}, {per_tip} mm per tip")
    return sensor, per_tip


class DailyRain:
    """Running rain total for the current UTC day, built from the increases between good reads."""

    def __init__(self, day, total):
        self.day, self.total, self.last = day, total, None

    def add_reading(self, now, cumulative):
        """Take one good gauge reading; returns the new day total when rain was added, else None."""
        if now.date() != self.day:  # new UTC day: the column's running total starts again from 0
            self.day, self.total = now.date(), 0.0
            print(f"New UTC day {self.day}: total reset to 0")
        if self.last is None:
            added = 0.0  # first good read: the gauge's count so far is the starting point
        elif cumulative >= self.last:
            added = cumulative - self.last
        else:
            added = cumulative  # the gauge restarted and counts from 0 again
            print(f"{now:%Y-%m-%d %H:%M:%S} gauge count went back from {self.last} to {cumulative} mm (gauge restarted)")
        self.last = cumulative  # always move on, so one odd reading can't block later ones
        if added > MAX_MM_PER_POLL:
            print(f"{now:%Y-%m-%d %H:%M:%S} ignored an implausible jump of {added:.2f} mm")
            return None
        if added > 0:
            self.total += added
            return self.total
        return None


def main():
    sensor, per_tip = setup()
    day = utc_now().date()
    rain = DailyRain(day, todays_total_from_csv(day))
    print(f"Today's total (UTC day {day}) so far: {rain.total:.4f} mm")
    while True:
        now = utc_now()
        try:
            tips, cumulative = read_gauge(sensor, per_tip)
        except (OSError, ValueError) as e:
            print(f"{now:%Y-%m-%d %H:%M:%S} skipped this minute: {e}")
        else:
            total = rain.add_reading(now, cumulative)
            if total is not None:
                log_to_csv(now.strftime("%Y-%m-%d %H:%M:%S"), total)
            print(f"{now:%Y-%m-%d %H:%M:%S} tips {tips}, gauge total {cumulative:.4f} mm, today {rain.total:.4f} mm")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
