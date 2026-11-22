#!/usr/bin/env python3
"""
ALREADY APPLIED on 2026-11-22; kept as the record of what was changed (see CLAUDE.md). Don't run it again
without reading it first.

One-off cleanup (2026-11-22) of the station's CSV archives, the live CSV and the SQLite DB, written to stream
the data (the first version held every row in memory and froze the Pi).

  python3 cleanup2.py            report only (writes per-month work files in WORK, changes nothing else)
  python3 cleanup2.py --apply    rewrite the CSVs, delete Unset_clock.csv, rebuild the DB table
"""
import argparse, glob, os, resource, shutil, sqlite3, statistics, sys, time
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timezone

LIMIT = 1_200_000_000
resource.setrlimit(resource.RLIMIT_AS, (LIMIT, LIMIT))   # fail with MemoryError rather than freeze the Pi

BASE = "/home/dimitris/weather_station"
LIVE = f"{BASE}/weather_station_data.csv"
ARCH = f"{BASE}/raw_data_months"
DB = "/home/dimitris/weather_station.db"
TABLE = "station_readings"
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cleanup_work")

T_BME, RH, P, LUX, LDET, LDIST, RAIN_EV, T_MCP, UV, RAIN, WS, WD, PM1, PM25, PM10 = range(1, 16)
NCOLS = 16
PI_COLS = (LDET, LDIST, RAIN, WS, WD, PM1, PM25, PM10)
BOARD_COLS = (T_BME, RH, P, T_MCP, LUX, UV, RAIN_EV)
TEXT_COLS = {LDET, RAIN_EV}
BME = (T_BME, RH, P)
EPOCH0 = datetime(1970, 1, 1)
# Board clocks that missed a daylight-saving change: (from, to, how far ahead the board rows are, seconds)
SHIFT_WINDOWS = [(datetime(2026, 3, 29), datetime(2026, 4, 2), -3600), (datetime(2026, 10, 25), datetime(2026, 10, 29), 3600)]
RANGES = {T_BME: (-25, 50), T_MCP: (-25, 50), RH: (0, 100), P: (950, 1060), LUX: (0, 150000), UV: (0, 15),
          LDIST: (0, 63), RAIN: (0, 300), WS: (0, 60), WD: (0, 360), PM1: (0, 1000), PM25: (0, 1000), PM10: (0, 1000)}
SPIKE = {T_BME: 6.0, RH: 25.0, P: 4.0}
ZERO_COLS = (T_MCP, T_BME)
BIAS_LIMIT = 3.0
UV_BAD_MONTHS = ("2025-04", "2025-05")


def epoch_of(s):
    """Seconds since 1970 for 'YYYY-MM-DD HH:MM:SS', -1 if unreadable, -2 if dated before 2020."""
    if len(s) < 19:
        return -1
    try:
        t = datetime.fromisoformat(s[:19])
    except ValueError:
        return -1
    return -2 if t.year < 2020 else int((t - EPOCH0).total_seconds())


def ts_of(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


def has(p, cols):
    return any(len(p) > c and p[c].strip() for c in cols)


def source_files():
    return sorted(f for f in glob.glob(f"{ARCH}/*.csv") if not f.endswith("Unset_clock.csv")) + [LIVE]


def read_lines(path, upto=None):
    """Header and the complete lines of a file, streamed. For the live CSV `upto` fixes the snapshot size."""
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8", "replace").rstrip("\r\n")
        pos = f.tell()
        for raw in f:
            pos += len(raw)
            if upto is not None and pos > upto:
                break
            if not raw.endswith(b"\n"):
                break
            yield raw.decode("utf-8", "replace").rstrip("\r\n")


def find_shifts(path, upto):
    """{line index: corrected epoch} for board rows written while a board's clock was an hour off."""
    ep, kind = array("q"), bytearray()       # kind: 1 Pi row, 2 board row, 0 other
    for line in read_lines(path, upto):
        p = line.split(",")
        e = epoch_of(p[0])
        ep.append(e)
        kind.append(0 if e < 0 else (1 if has(p, PI_COLS) else (2 if has(p, BOARD_COLS) else 0)))
    n = len(ep)
    prev_pi, next_pi = array("q", [-1]) * n, array("q", [-1]) * n
    last = -1
    for i in range(n):
        prev_pi[i] = last
        if kind[i] == 1: last = ep[i]
    last = -1
    for i in range(n - 1, -1, -1):
        next_pi[i] = last
        if kind[i] == 1: last = ep[i]
    shifts, ambiguous = {}, Counter()
    windows = [(int((a - EPOCH0).total_seconds()), int((b - EPOCH0).total_seconds()), off) for a, b, off in SHIFT_WINDOWS]
    for i in range(n):
        if kind[i] != 2 or prev_pi[i] < 0 or next_pi[i] < 0:
            continue
        for a, b, off in windows:
            if a <= ep[i] < b:
                lo, hi = ep[i] - next_pi[i] - 60, ep[i] - prev_pi[i] + 60
                if lo <= off <= hi and not lo <= 0 <= hi:
                    shifts[i] = ep[i] - off
                elif not lo <= 0 <= hi:
                    ambiguous[ts_of(ep[i])[:10]] += 1
    return shifts, ambiguous


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true"); args = ap.parse_args()
    t0 = time.time()
    files = source_files()
    live_upto = os.path.getsize(LIVE)
    live_upto = live_upto  # snapshot: only complete lines up to this size are used
    upto = {f: (live_upto if f == LIVE else None) for f in files}
    header = next(iter([open(LIVE, encoding="utf-8").readline().rstrip("\r\n")]))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            assert fh.readline().rstrip("\r\n") == header, f"header differs in {f}"

    # A. Clock shifts (file order), then the series needed for neighbour-based checks
    shifts = {}
    for f in files:
        shifts[f], amb = find_shifts(f, upto[f])
        for d, c in sorted(amb.items()):
            print(f"  ambiguous rows (neither on time nor an hour off) {os.path.basename(f)} {d}: {c}")
    shift_days = Counter(ts_of(e)[:10] for s in shifts.values() for e in s.values())
    print(f"timestamps to correct: {sum(shift_days.values())} {dict(sorted(shift_days.items()))}  ({time.time()-t0:.0f} s)")

    ser = {c: (array("q"), array("d")) for c in set(SPIKE) | set(ZERO_COLS)}
    for f in files:
        sh = shifts[f]
        for i, line in enumerate(read_lines(f, upto[f])):
            p = line.split(",")
            e = epoch_of(p[0])
            if e < 0: continue
            e = sh.get(i, e)
            for c, (ea, va) in ser.items():
                if len(p) > c and p[c].strip():
                    v = num(p[c])
                    if v is not None: ea.append(e); va.append(v)

    # B. Neighbour-based flags: {(col, epoch, value)}
    flags = set(); why = Counter()
    for c, (ea, va) in ser.items():
        pts = sorted(set(zip(ea, va)))              # overlapping archives hold identical copies
        if c in ZERO_COLS:
            last_nz = None
            for e, v in pts:
                if v == 0.0 and last_nz is not None and abs(last_nz) >= 2:
                    flags.add((c, e, v)); why[f"{c}: 0.00 glitch"] += 1
                if v != 0.0: last_nz = v
        if c in SPIKE:
            lim = SPIKE[c]; vals = [v for _, v in pts]
            for i in range(3, len(pts) - 3):
                if pts[i + 3][0] - pts[i - 3][0] > 3600: continue
                nb = vals[i - 3:i] + vals[i + 1:i + 4]
                if max(nb) - min(nb) < lim and abs(vals[i] - statistics.median(nb)) > lim:
                    flags.add((c, pts[i][0], vals[i])); why[f"{c}: spike"] += 1
        del pts
    ser.clear()
    print(f"neighbour-based flags: {dict(why)}  ({time.time()-t0:.0f} s)")

    def clean(p, e):
        """(cleaned field list or None to drop the row, Counter of what was done)."""
        done = Counter()
        q = list(p) + [""] * (NCOLS - len(p))
        q[0] = ts_of(e)
        vals = {c: num(q[c]) for c in range(1, NCOLS) if c not in TEXT_COLS and q[c].strip()}
        bad = set()
        for c, v in vals.items():
            if v is None: bad.add(c); done[f"not a number: col {c}"] += 1; continue
            lo, hi = RANGES.get(c, (None, None))
            if lo is not None and not lo <= v <= hi: bad.add(c); done[f"out of range: col {c}"] += 1
            if (c, e, v) in flags: bad.add(c); done[f"glitch/spike: col {c}"] += 1
        if vals.get(T_BME) == 85.0 or vals.get(P) == 867.77:
            bad.add(T_BME); done["BME280 reset value"] += 1
        if bad & set(BME):
            bad |= {c for c in BME if q[c].strip()}
        tb, tm = vals.get(T_BME), vals.get(T_MCP)
        if tb is not None and tm is not None and T_BME not in bad and T_MCP not in bad and abs(tb - tm) > BIAS_LIMIT:
            bad |= {c for c in (T_BME, RH) if q[c].strip()}; done["BME280 off the MCP9808 by > 3 °C"] += 1
        if q[UV].strip() and q[0][:7] in UV_BAD_MONTHS:
            bad.add(UV); done["UV Apr-May 2025"] += 1
        for c in bad:
            if q[c].strip(): q[c] = ""; done["values blanked"] += 1
        q = q[:max(len(p), 1)]
        if not any(x.strip() for x in q[1:]):
            done["rows dropped (no values left)"] += 1
            return None, done
        return q, done

    # C. Clean every file (to temp files when applying) and write per-month work files for the DB
    for sub in ("raw", "clean"):
        d = os.path.join(WORK, sub)
        if os.path.isdir(d): shutil.rmtree(d)
        os.makedirs(d)
    month_fh = {}
    def month_out(kind, m):
        k = (kind, m)
        if k not in month_fh:
            if len(month_fh) > 60:
                for fh in month_fh.values(): fh.close()
                month_fh.clear()
            month_fh[k] = open(os.path.join(WORK, kind, m + ".csv"), "a", encoding="utf-8")
        return month_fh[k]
    totals = Counter(); per_file = {}
    for f in files:
        sh = shifts[f]; out = None; changed = 0; dropped = Counter()
        if args.apply and f != LIVE:
            out = open(f + ".tmp", "w", encoding="utf-8"); out.write(header + "\n")
        if args.apply and f == LIVE:
            out = open(f + ".tmp", "w", encoding="utf-8"); out.write(header + "\n")
        for i, line in enumerate(read_lines(f, upto[f])):
            p = line.split(",")
            e0 = epoch_of(p[0])
            if e0 == -1: dropped["unreadable timestamp"] += 1; changed += 1; continue
            if e0 == -2: dropped["dated before 2020"] += 1; changed += 1; continue
            month_out("raw", p[0][:7]).write(line + "\n")
            e = sh.get(i, e0)
            if e != e0: totals["timestamps corrected"] += 1
            q, done = clean(p, e)
            totals.update(done)
            if q is None: changed += 1; continue
            new = ",".join(q)
            if new != line: changed += 1
            month_out("clean", q[0][:7]).write(new + "\n")
            if out: out.write(new + "\n")
        totals.update(dropped)
        per_file[f] = (changed, dropped)
        if out: out.close()
    for fh in month_fh.values(): fh.close()
    print("\nper file: lines changed or dropped")
    for f, (c, d) in per_file.items(): print(f"  {os.path.basename(f):28s} {c:7,}  {dict(d) if d else ''}")
    print("totals:", dict(totals), f"({time.time()-t0:.0f} s)")

    if args.apply:
        apply_changes(files, header, live_upto, clean, t0)
        return

    # D. Compare with the DB, month by month
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    def norm(c, v):
        if v is None: return None
        s = str(v).strip()
        if s in ("", "nan", "None"): return None
        if c in TEXT_COLS: return s
        x = num(s)
        return round(x, 6) if x is not None else s
    def norm_csv(line):
        p = line.split(",")
        return (p[0][:19],) + tuple(norm(c, p[c] if len(p) > c else "") for c in range(1, NCOLS))
    def norm_db(r):
        return (str(r[0])[:19],) + tuple(norm(c, r[c]) for c in range(1, NCOLS))
    months = sorted(set(m[:-4] for m in os.listdir(os.path.join(WORK, "raw"))) | set(m[:-4] for m in os.listdir(os.path.join(WORK, "clean"))))
    print("\nmonth    CSV rows   DB rows  missing in DB  DB-only  DB duplicates  clean rows")
    db_only_all = 0
    for m in months:
        rp, cp = os.path.join(WORK, "raw", m + ".csv"), os.path.join(WORK, "clean", m + ".csv")
        raw = set(norm_csv(l.rstrip("\n")) for l in open(rp, encoding="utf-8")) if os.path.exists(rp) else set()
        clean_n = len(set(open(cp, encoding="utf-8"))) if os.path.exists(cp) else 0
        y, mo = map(int, m.split("-")); nxt = f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"
        dbc = Counter(norm_db(r) for r in con.execute(f"SELECT * FROM {TABLE} WHERE timestamp >= ? AND timestamp < ?", (m, nxt)))
        only = set(dbc) - raw
        db_only_all += len(only)
        print(f"{m}  {len(raw):9,} {sum(dbc.values()):9,} {len(raw - set(dbc)):13,} {len(only):8,} {sum(c - 1 for c in dbc.values()):13,} {clean_n:11,}")
        for r in sorted(only, key=repr)[:2]: print("        DB-only e.g.", r[:10])
        del raw, dbc
    print(f"({time.time()-t0:.0f} s)")


def csv_text(c, v):
    """A DB value written back as CSV text (wind direction as a whole number, like wind.py writes it)."""
    if v is None: return ""
    if isinstance(v, str): return v
    x = float(v)
    return str(int(x)) if c == WD and x == int(x) else repr(x)


def db_value(c, s):
    s = s.strip()
    if s == "": return None
    if c in TEXT_COLS: return s
    return num(s)


def apply_changes(files, header, live_upto, clean, t0):
    raw_dir, clean_dir = os.path.join(WORK, "raw"), os.path.join(WORK, "clean")
    snap_max = max(l[:19] for l in read_lines(LIVE, live_upto) if epoch_of(l[:19]) >= 0)
    print(f"\nAPPLY. Live CSV snapshot ends at {snap_max} UTC")

    # 1. Rows that only the DB has (their time is in no CSV): restore them, cleaned, into the CSVs and the DB
    con = sqlite3.connect(DB, timeout=60)
    restored = defaultdict(list)          # month -> cleaned CSV lines
    for m in sorted(f[:-4] for f in os.listdir(raw_dir)):
        ts = set(l[:19] for l in open(os.path.join(raw_dir, m + ".csv"), encoding="utf-8"))
        y, mo = map(int, m.split("-")); nxt = f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"
        for r in con.execute(f"SELECT * FROM {TABLE} WHERE timestamp >= ? AND timestamp < ?", (m, nxt)):
            t = str(r[0])[:19]
            if t in ts or t > snap_max: continue
            p = [t] + [csv_text(c, v) for c, v in enumerate(r[1:], 1)]
            while len(p) > 1 and p[-1] == "": p.pop()
            q, _ = clean(p, epoch_of(t))
            if q: restored[t[:7]].append(",".join(q))
    print("rows restored from the DB:", {m: len(v) for m, v in restored.items()})

    # 2. Put the restored rows into the archive (temp) file that covers their time, then swap the archives in
    spans = {}
    for f in files:
        if f == LIVE: continue
        times = [l[:19] for l in open(f + ".tmp", encoding="utf-8") if epoch_of(l[:19]) >= 0]
        spans[f] = (min(times), max(times)) if times else ("9", "0")
    for m, lines in restored.items():
        for line in lines:
            target = next((f for f, (lo, hi) in spans.items() if lo <= line[:19] <= hi), None)
            target = target or os.path.join(ARCH, f"{['January','February','March','April','May','June','July','August','September','October','November','December'][int(m[5:7]) - 1]}_{m[:4]}.csv")
            if target == LIVE or not os.path.exists(target + ".tmp"):
                print("  no archive covers", line[:19], "- left out of the CSVs"); continue
            with open(target + ".tmp", "a", encoding="utf-8") as fh: fh.write(line + "\n")
        with open(os.path.join(clean_dir, m + ".csv"), "a", encoding="utf-8") as fh:
            fh.write("".join(l + "\n" for l in lines))
    for f in files:
        if f == LIVE: continue
        shutil.copymode(f, f + ".tmp")
        os.replace(f + ".tmp", f)
    print(f"archives rewritten ({time.time()-t0:.0f} s)")

    # 3. The live CSV: cleaned snapshot + rows written since, swapped in; rows written around the swap copied over
    with open(LIVE, "rb") as old:
        old.seek(live_upto)
        since = old.read(); since = since[:since.rfind(b"\n") + 1]
        with open(LIVE + ".tmp", "ab") as fh:
            fh.write(since); fh.flush(); os.fsync(fh.fileno())
        pos = live_upto + len(since)
        shutil.copymode(LIVE, LIVE + ".tmp")
        os.replace(LIVE + ".tmp", LIVE)
        late_n = 0
        for _ in range(2):
            time.sleep(5)
            old.seek(pos); late = old.read(); late = late[:late.rfind(b"\n") + 1]
            if late:
                with open(LIVE, "ab") as fh: fh.write(late)
                pos += len(late); late_n += late.count(b"\n")
    n_since = since.count(b"\n")
    print(f"live CSV swapped in ({n_since} rows written during the run, {late_n} during the swap)")
    unset = os.path.join(ARCH, "Unset_clock.csv")
    if os.path.exists(unset):
        os.remove(unset); print("deleted", unset)

    # 4. Rebuild the DB table from the cleaned rows (each row once), keep rows newer than the snapshot
    create = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (TABLE,)).fetchone()[0]
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({TABLE})")]
    con.execute(f"DROP TABLE IF EXISTS {TABLE}_new")
    con.execute(create.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {TABLE}_new", 1))
    con.commit()
    marks = ",".join("?" * len(cols))
    total = 0
    for m in sorted(f[:-4] for f in os.listdir(clean_dir)):
        rows = {}
        for line in open(os.path.join(clean_dir, m + ".csv"), encoding="utf-8"):
            p = line.rstrip("\n").split(",")
            p += [""] * (NCOLS - len(p))
            r = (p[0][:19] + ".000000",) + tuple(db_value(c, p[c]) for c in range(1, NCOLS))
            rows[(r[0],) + tuple(round(v, 6) if isinstance(v, float) else v for v in r[1:])] = r
        con.executemany(f"INSERT INTO {TABLE}_new VALUES ({marks})", sorted(rows.values(), key=lambda r: r[0]))
        con.commit()
        total += len(rows)
        del rows
    print(f"new table: {total:,} cleaned rows ({time.time()-t0:.0f} s)")
    con.execute("BEGIN IMMEDIATE")
    newer = con.execute(f"INSERT INTO {TABLE}_new SELECT * FROM {TABLE} WHERE timestamp > ?", (snap_max + ".999999",)).rowcount
    con.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_old")
    con.execute(f"ALTER TABLE {TABLE}_new RENAME TO {TABLE}")
    con.execute(f"DROP TABLE {TABLE}_old")
    con.commit()
    n, lo, hi = con.execute(f"SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM {TABLE}").fetchone()
    print(f"DB swapped: {newer} rows newer than the snapshot kept; now {n:,} rows, {lo} .. {hi} ({time.time()-t0:.0f} s)")


if __name__ == "__main__":
    main()
