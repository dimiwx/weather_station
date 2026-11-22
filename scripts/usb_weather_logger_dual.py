#!/usr/bin/env python3
"""
Reads the two ESP32 boards over USB serial, converts their local timestamps to UTC and appends their CSV
rows to weather_station_data.csv. Runs from ~/auto_usb_logger.sh, which restarts it if it ever exits.

Robustness (2026-11-19):
- A read error (cable glitch, USB reset, a second program on the port) no longer ends a board's reader:
  it closes the port and tries to reopen it every RETRY_S seconds until it works again.
- While another program has a board's port open (e.g. Thonny, or a diagnostic script), the logger closes
  that port and waits until the program has closed it, then resumes. So Thonny can be used without
  stopping the logger.
- If a reader thread dies anyway, the whole logger exits so the wrapper restarts it, instead of silently
  carrying on with one board (which is how the UV board's data was lost for 35 h in November 2026).
- Events go to EVENT_LOG: errors, reconnects, other programs on a port, lines that aren't CSV rows, and
  gaps of over ROW_GAP_LOG_S in a board's rows (with how many lines arrived meanwhile).
"""
import os
import re
import sys
import time
import argparse
import threading
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import serial


# ----------------------------
# Defaults
# ----------------------------
DEFAULT_OUTFILE = "/home/dimitris/weather_station/weather_station_data.csv"
DEFAULT_EVENT_LOG = "/home/dimitris/usb_weather_logger.log"
DEFAULT_BAUD = 115200

DEFAULT_INPUT_TZ = "Europe/Athens"   # EET/EEST (DST handled automatically); Thonny sets the boards' clocks to local time
DEFAULT_OUTPUT_TZ = "UTC"

# Physical USB sockets (by-path), not /dev/ttyUSB* numbers: those swap between boots.
DEFAULT_PORT_A = "/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0-port0"  # UV/light/rain board
DEFAULT_PORT_B = "/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-port0"  # BME280/MCP9808 board

# A board whose clock was never set (e.g. after a reset) counts from 2000-01-01; rows dated before this are dropped.
MIN_VALID_YEAR = 2020

# CSV lines start with: YYYY-MM-DD HH:MM:SS,
CSV_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},")
CSV_TS_ANYWHERE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},")
# The boards' USB link sometimes delivers a corrupted packet: 64 bytes that aren't valid text (U+FFFD once
# decoded) at the start of a burst of output (230 times in 45 h on the UV board in November 2026). When it
# lands in front of a data row, the row behind it is intact, so it's recovered (see strip_garbage).


def strip_garbage(line):
    """(row, number of garbage characters) when `line` is garbage followed by an intact CSV row, else None.
    Only undecodable bytes, control characters and spaces count as garbage, and the row itself must be clean."""
    m = CSV_TS_ANYWHERE_RE.search(line)
    if not m or m.start() == 0:
        return None
    prefix, row = line[:m.start()], line[m.start():]
    if "�" in row or not all(c == "�" or ord(c) < 32 or c.isspace() for c in prefix):
        return None
    return row, len(prefix)
# Routine non-CSV output that isn't worth logging (the UV board's per-cycle debug and file-health lines)
ROUTINE_LINE_RE = re.compile(r"^(Time: |LOG HEALTH: |PAD )")
# What is left of a routine line once a corrupted packet has eaten its start: the temperature board's padding
# (dashes, since 2026-11-22) or the end of the UV board's debug line. Counted, and reported once an hour.
ROUTINE_FRAGMENT_RE = re.compile(r"^(PAD )?-*$|RawV: ")
CORRUPTION_REPORT_S = 3600

# The MCP9808 reads exactly 0.00 when the temperature board's I2C bus glitches (10 times from April to
# November 2026, each right after 11-28 °C). Such a reading is left out when the last accepted MCP9808
# reading (or, without a recent one, the BME280 temperature in the same row) is at least
# ZERO_GLITCH_JUMP away from 0, so genuine readings near 0 °C on a cold night are kept.
MCP_FIELD, BME_TEMP_FIELD = 8, 1   # positions in the temperature board's CSV row
ZERO_GLITCH_JUMP = 2.0             # °C
ZERO_GLITCH_REF_S = 20 * 60        # how recent the last accepted reading must be to compare with

RETRY_S = 10              # wait between attempts to reopen a port after an error
PORT_CHECK_S = 3          # how often to look for other programs on the ports
ROW_GAP_LOG_S = 300       # log when a board sends no CSV row for longer than this
EVENT_LOG_MAX = 1_000_000  # bytes; the event log is cut to its newer half beyond this

_event_lock = threading.Lock()
EVENT_LOG = Path(DEFAULT_EVENT_LOG)


def log_event(message: str):
    """Prints a line and appends it, with the local time, to EVENT_LOG."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    with _event_lock:
        try:
            with EVENT_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if EVENT_LOG.stat().st_size > EVENT_LOG_MAX:
                data = EVENT_LOG.read_bytes()[-EVENT_LOG_MAX // 2:]
                EVENT_LOG.write_bytes(data[data.find(b"\n") + 1:])
        except OSError as e:
            print(f"(could not write the event log: {e})", flush=True)


def convert_athens_to_utc(ts_str: str, input_tz: ZoneInfo, output_tz: ZoneInfo) -> str:
    local_naive = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    local_dt = local_naive.replace(tzinfo=input_tz)
    utc_dt = local_dt.astimezone(output_tz)
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S")


def process_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()[:80] or "?"
    except OSError:
        return "?"


def port_users(devices):
    """{device path: [pids of other processes that have it open]} (only processes we may inspect)."""
    users = {d: [] for d in devices}
    mine = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == mine:
            continue
        try:
            fds = os.listdir(f"/proc/{entry}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{entry}/fd/{fd}")
            except OSError:
                continue
            if target in users:
                users[target].append(int(entry))
    return users


class Reader(threading.Thread):
    """Reads one board's serial port and appends its rows to the CSV, reconnecting after errors."""

    def __init__(self, port, baud, label, outfile, input_tz, output_tz, keep_bad, csv_lock):
        super().__init__(name=label, daemon=True)
        self.port, self.baud, self.label = port, baud, label
        self.outfile, self.input_tz, self.output_tz = outfile, input_tz, output_tz
        self.keep_bad, self.csv_lock = keep_bad, csv_lock
        self.busy = threading.Event()   # set while another program has the port open
        self.problem = None             # the last problem logged, so retries are logged only once
        self.no_clock = False
        self.last_row = None            # time.monotonic() of the last CSV row written
        self.lines_since_row = 0
        self.other_since_row = 0
        self.last_mcp = None            # (time.monotonic(), value) of the last accepted MCP9808 reading
        self.absorbed = 0               # corrupted packets that only hit a routine line, since the last report
        self.absorbed_since = time.monotonic()

    def filter_zero_glitch(self, parts):
        """Blanks an MCP9808 reading of exactly 0.00 that is a bus glitch (see ZERO_GLITCH_JUMP)."""
        if len(parts) <= MCP_FIELD or not parts[MCP_FIELD].strip():
            return
        try:
            value = float(parts[MCP_FIELD])
        except ValueError:
            return
        now = time.monotonic()
        if value == 0.0:
            ref = None
            if self.last_mcp and now - self.last_mcp[0] <= ZERO_GLITCH_REF_S:
                ref, source = self.last_mcp[1], "the previous MCP9808 reading"
            elif parts[BME_TEMP_FIELD].strip():
                try:
                    ref, source = float(parts[BME_TEMP_FIELD]), "the BME280 in the same row"
                except ValueError:
                    pass
            if ref is not None and abs(ref) >= ZERO_GLITCH_JUMP:
                parts[MCP_FIELD] = ""
                log_event(f"[{self.label}] MCP9808 read 0.00 while {source} was {ref:.2f} °C: "
                          "a bus glitch, value left out of the CSV")
                return
        self.last_mcp = (now, value)

    def report(self, problem):
        if problem != self.problem:
            log_event(f"[{self.label}] {problem}")
            self.problem = problem

    def run(self):
        log_event(f"[{self.label}] reading {self.port} @ {self.baud}")
        while True:
            if self.busy.is_set():
                time.sleep(1)
                continue
            try:
                ser = serial.Serial(self.port, self.baud, timeout=1)
            except (serial.SerialException, OSError) as e:
                self.report(f"cannot open the port ({e}); retrying every {RETRY_S} s")
                time.sleep(RETRY_S)
                continue
            if self.problem:
                log_event(f"[{self.label}] port open again")
                self.problem = None
            try:
                with ser:
                    while not self.busy.is_set():
                        raw = ser.readline()
                        if raw:
                            self.handle(raw)
            except (serial.SerialException, OSError) as e:
                self.report(f"read error ({e}); reopening every {RETRY_S} s")
                time.sleep(RETRY_S)

    def append(self, text):
        with self.csv_lock:
            with self.outfile.open("a", buffering=1, encoding="utf-8") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())

    def handle(self, raw):
        """One line from the board. Errors here are logged and never stop the reader."""
        try:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                return
            self.lines_since_row += 1

            # Only CSV lines that start with a timestamp are data rows (or that do once garbage is removed)
            if not CSV_TS_RE.match(line):
                recovered = strip_garbage(line)
                if recovered:
                    line, garbage = recovered
                    log_event(f"[{self.label}] row recovered from behind {garbage} garbage bytes: {line[:80]!r}")
            if not CSV_TS_RE.match(line):
                self.other_since_row += 1
                rest = line.lstrip("�")
                if ROUTINE_LINE_RE.match(line):
                    pass
                elif rest != line and ROUTINE_FRAGMENT_RE.search(rest):
                    self.absorbed += 1
                else:
                    log_event(f"[{self.label}] not a CSV row: {line[:200]!r}")
                now = time.monotonic()
                if now - self.absorbed_since >= CORRUPTION_REPORT_S:
                    if self.absorbed:
                        log_event(f"[{self.label}] {self.absorbed} corrupted packets hit only routine lines in the "
                                  f"last {(now - self.absorbed_since) / 3600:.1f} h (no data lost)")
                    self.absorbed, self.absorbed_since = 0, now
                if self.keep_bad:
                    self.append(f"# [{self.label}] {line}")
                return

            parts = line.split(",")
            ts_local = parts[0].strip()
            if int(ts_local[:4]) < MIN_VALID_YEAR:
                print(f"[{self.label}] clock not set, skipping: {line}", flush=True)
                if not self.no_clock:
                    log_event(f"[{self.label}] the board's clock is not set (it was reset?): rows are skipped "
                              "until Thonny sets it")
                    self.no_clock = True
                if self.keep_bad:
                    self.append(f"# [{self.label}] NOCLOCK {line}")
                return
            if self.no_clock:
                log_event(f"[{self.label}] the board's clock is set again")
                self.no_clock = False

            parts[0] = convert_athens_to_utc(ts_local, self.input_tz, self.output_tz)
            self.filter_zero_glitch(parts)
            out_line = ",".join(parts)
            self.append(out_line)
            print(f"[{self.label}] {out_line}", flush=True)

            now = time.monotonic()
            if self.last_row is not None and now - self.last_row > ROW_GAP_LOG_S:
                log_event(f"[{self.label}] no row for {now - self.last_row:.0f} s before this one "
                          f"({self.lines_since_row - 1} other lines arrived meanwhile, "
                          f"{self.other_since_row} of them not CSV rows)")
            self.last_row = now
            self.lines_since_row = 0
            self.other_since_row = 0
        except Exception as e:
            log_event(f"[{self.label}] error handling {raw[:200]!r}: {type(e).__name__}: {e}")


def main():
    global EVENT_LOG
    ap = argparse.ArgumentParser(
        description="Dual USB logger for two ESP32s: read two serial ports concurrently, convert Athens->UTC, append to one CSV."
    )

    ap.add_argument("--port-a", default=DEFAULT_PORT_A, help="First serial port (default: UV/light/rain board, USB socket 1-1.4).")
    ap.add_argument("--id-a", default="ESP32_RAD", help="Label for port A (only used in console/log messages).")
    ap.add_argument("--port-b", default=DEFAULT_PORT_B, help="Second serial port (default: BME280/MCP9808 board, USB socket 1-1.2).")
    ap.add_argument("--id-b", default="ESP32_TEMP", help="Label for port B (only used in console/log messages).")

    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Baud rate (default 115200).")
    ap.add_argument("--outfile", default=DEFAULT_OUTFILE, help="Output CSV path.")
    ap.add_argument("--event-log", default=DEFAULT_EVENT_LOG, help="Where errors, reconnects and gaps are logged.")
    ap.add_argument("--input-tz", default=DEFAULT_INPUT_TZ, help=f"Incoming timestamp tz (default {DEFAULT_INPUT_TZ}).")
    ap.add_argument("--output-tz", default=DEFAULT_OUTPUT_TZ, help="Output tz (default UTC).")
    ap.add_argument("--keep-bad", action="store_true", help="Also write non-CSV lines into the CSV as comments starting with #.")
    args = ap.parse_args()

    EVENT_LOG = Path(args.event_log)
    outfile = Path(args.outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    print("Writing to:", outfile)
    print(f"Convert {args.input_tz} -> {args.output_tz}")
    print("A:", args.port_a, "ID:", args.id_a)
    print("B:", args.port_b, "ID:", args.id_b)
    print("Events are logged to:", EVENT_LOG)

    csv_lock = threading.Lock()
    readers = [
        Reader(port, args.baud, label, outfile, ZoneInfo(args.input_tz), ZoneInfo(args.output_tz), args.keep_bad, csv_lock)
        for port, label in ((args.port_a, args.id_a), (args.port_b, args.id_b))
    ]
    log_event(f"logger started (pid {os.getpid()})")
    for r in readers:
        r.start()

    # Watch the readers, and pause a reader while another program has its port open
    while True:
        dead = [r.label for r in readers if not r.is_alive()]
        if dead:
            log_event(f"reader {', '.join(dead)} stopped unexpectedly; exiting so the wrapper restarts the logger")
            sys.exit(1)
        try:
            devices = {r.label: os.path.realpath(r.port) for r in readers}
            users = port_users(set(devices.values()))
            for r in readers:
                pids = users.get(devices[r.label], [])
                if pids and not r.busy.is_set():
                    r.busy.set()
                    log_event(f"[{r.label}] another program has the port open, pausing until it closes it: "
                              + "; ".join(f"pid {p} {process_name(p)}" for p in pids))
                elif not pids and r.busy.is_set():
                    r.busy.clear()
                    log_event(f"[{r.label}] the other program closed the port; resuming")
        except Exception as e:
            print(f"port check failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(PORT_CHECK_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_event("logger stopped (Ctrl-C)")
        raise
