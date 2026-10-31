# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Krini Weather Station is a Raspberry Pi 4 weather station in Kalamaria, Thessaloniki. It serves a public Dash dashboard at weather-krini.ngrok.io (ngrok → port 8050). The station uses sensors attached to the Pi (wind, PMSA003I air quality, AS3935 lightning, SEN0575 tipping-bucket rain, zenith sky camera) and remote ESP32 boards (BME280, MCP9808, UV, light, LM393 rain). All code is Python plus a few bash loop wrappers. There is no build, lint, or test suite. To check a change, run the script directly with `python3 <script>.py`.

## Where the running code actually lives

This matters more than anything else here. The repo is **not** the single source of truth for the production scripts:

- **Sensor scripts run from `/home/dimitris/`, not from this repo.** These include `wind.py`, `air_quality.py`, `rainfall2.py`, `lightning_detector2.py` (via `auto_lightning.sh`), `camera_capture.sh`, `download_satellite2.sh`, and `upload_WU.py`. `scripts/` holds git-tracked snapshots of them, and several have drifted from the live copies. Before editing, diff against `~/<file>` and ask which copy should change.
- **`~/air_quality_new.py` is the redesigned PMSA003I logger, waiting to replace `~/air_quality.py`.** It averages 1-s frames per minute, uses the atmospheric ("env") values in µg/m³ without the old `/10`, validates frames, and writes `air_quality_details.csv`. `--dry-run` prints the averages without writing anything. It stays out of service until the sensor is cleaned, because the sensor currently reads ~14× the CAMS model (PM2.5 ~93 vs ~7 µg/m³). The live script's `/10` hides that, and the true values would reach the public dashboard and Weather Underground.
- **Scripts that run from the repo root:** `auto_plotly.py` → `timeseries_plotly_optimised.py`, `auto_telegram.sh` → `notif_telegram.py`, plus the cron jobs below. The root copies are the live ones. The copies in `scripts/` are older snapshots.
- **Startup:** `weatherstation.service` (systemd) runs `~/run_weather_station.sh`. That script opens each long-running process in its own `gnome-terminal`, with cwd `~` for the sensor scripts and `~/weather_station` for the Plotly and Telegram scripts.
- **Cron** (`crontab -l`):
  - `upload_database_weather_station.py` every 30 minutes
  - `plot_skewt.py` every 2 hours at :30 (it exits early when the newest sounding is already plotted)
  - `get_anomaly.py` at 08:00
  - `7-day_forecast.py` at 07:00 and 19:00
  - `nowcast.py` is not scheduled at the moment (its dashboard tab is hidden too)

  Cron runs with cwd `~`, so scripts that use relative paths resolve them against the home directory. For example, the live SQLite DB is `~/weather_station.db` (about 1.9 GB), and the `weather_station.db` in the repo is a stale copy.
- `timeseries_plotly.py`, `Procfile`, and `requirements.txt` belong to an older Heroku deployment (the `heroku` git remote). They do not reflect what runs on the Pi.

## Data flow

Everything goes through one append-only CSV: `/home/dimitris/weather_station/weather_station_data.csv`.

- **Writers:** each sensor script on the Pi, plus the ESP32 boards. The boards print pre-formatted CSV lines over **USB serial** (115200 baud). `~/usb_weather_logger_dual.py` reads both boards, converts their timestamps from local time to UTC, and appends the lines. The boards' USB firmware is not in this repo: copies of both boards' files (2026-09-29) are in `~/esp32_backup/`, and the current UV-board source is `~/esp32_firmware/uv_board/main.py`. Both contain the Wi-Fi password, so keep them out of git. The UV board saves its UV dark baseline to `vdark.txt` at 03:00 each night and loads it at boot, because a daylight calibration zeroes the UV index. It writes no CSV until the board's clock is set, which currently only happens when Thonny connects (in local time), so any reset silences a board. USB-serial devices must be addressed by physical port, never by `ttyUSB` number, because the numbers swap between boots (see MIGRATION.md). The older paths are legacy and no longer used: TCP port 12345 (`listen.py`) and BLE (`listen_BLE.py`). Every writer appends a **sparse row**: the timestamp plus only its own columns, with the rest left empty. Rows from different writers are interleaved and **not strictly time-ordered**, so consumers must sort and resample, never assume one row holds a full observation.
- **Air temperature comes from the MCP9808.** In mid-2026 the BME280 read several °C high with implausibly moist humidity. It stopped answering on I²C on 2026-10-11 and isn't detected at boot, so humidity and pressure have been missing since then. The temperature board still sends its MCP9808 rows with the BME280 fields empty. The dashboard's thermometer gauge and the Telegram min/max use `Temperature (MCP9808) (°C)`, and the BME280 temperature is hidden on the dashboard by `SHOW_BME280_TEMPERATURE = False` in `timeseries_plotly_optimised.py`. Set it back to `True` once the BME280 board is replaced.
- **Wind direction:** the vane reports only 8 positions, 45° apart. `wind.py` stores `(raw - 90 + 23) % 360`, where -90 is for the mounting and +23 (from 2026-10-31 13:35 UTC) comes from a 14-day comparison with Open-Meteo. The +23 most likely corrects the vane reporting the start of each 45° sector instead of its centre. Rows before then lack the +23. Because values now sit at 23°, 68°…, any binning must use 16 directions (as the dashboard's wind rose does), or each value lands one 8-direction bin clockwise. Average directions as vectors, never arithmetically.
- **Column names** are the header of that CSV. They are long names that include the sensor, e.g. `Temperature (BME280) (°C)`. The dashboard, the Telegram notifier, the nowcast, and the DB uploader all hard-code these strings, so renaming a column breaks every one of them.
- **Timestamps** are UTC, written as `%Y-%m-%d %H:%M:%S`. The dashboard converts them to local time for display, and it drops timestamps in the future caused by NTP glitches.
- **Readers:**
  - `timeseries_plotly_optimised.py` is the live Dash app (rewritten 2026-09-29; the previous version is `timeseries_plotly_optimised.py.bak-2026-09-29`). How it stays light on the Pi and on ngrok:
    - It parses the last ~800 KB of the CSV (24 h window) only when the file changed, under a lock (parallel callbacks otherwise got empty data).
    - Browsers poll every 30 s. A tab re-renders only when its `content_version` changes (newest reading of its columns, or image mtime), and each version is rendered once for all viewers (`_render_cache`).
    - Charts plot each sensor's own readings via `series()`, never the sparse rows directly, averaged for wind (10 min) and PM (5 min). `with_gaps()` breaks lines at outages.
    - Images are served by the `/media/<name>` Flask route, scaled to 1600 px and cached by the browser via `?v=<mtime>`. Never base64-embed them in callbacks.
    - An `after_request` hook gzips text responses.
    - `SHOW_NOWCAST_TAB` (off) and `SHOW_BME280_TEMPERATURE` (off) toggle those views.
    - The Satellite and Cloud Camera tabs play loops through a **clientside** callback, so stepping frames costs the Pi nothing. `animation(prefix, ...)` builds a loop and `register_animation(prefix, hold)` registers its callback (`<prefix>-timer` → `<prefix>-frame`); the newest frame stays for 1 + hold ticks. Components inside tabs need `suppress_callback_exceptions=True`, which is set.
      - Satellite (`sat`): the last `SATELLITE_FRAMES` (5) frames, 800 ms each, from `/satellite-frame/<name>`.
      - Cloud Camera (`cam`): the last `CAMERA_FRAMES` (10) regular photos (not `THUNDER*`), 400 ms each, with a 2-s pause on the newest (`CAMERA_PAUSE_MS`). They're picked by file time, because the `DD-MM-YYYY_…` names don't sort by date, and photos younger than 15 s are skipped while they're still being written. They're served from `/camera-frame/<name>`, which only serves the current 10, scaled to 1600 px and cached in memory.
      - Both routes send immutable cache headers.
    - The "The Station" tab shows `assets/station_photo.jpg`. Replace that file to change the photo; its file time is part of the URL, so browsers fetch the new one.
  - `notif_telegram.py` is an async loop that sends threshold alerts to Telegram.
  - `upload_database_weather_station.py` appends any row newer than `MAX(timestamp)` to the SQLite table `station_readings`.
  - `nowcast.py` and `upload_WU.py` also read the CSV.
- Monthly CSVs are periodically split out into `raw_data_months/`.

## Derived products (each writes into a folder the dashboard reads)

| Script | Source | Output |
|---|---|---|
| `plot_skewt.py` | University of Wyoming soundings, station 16622, fetched as CSV from `weather.uwyo.edu/wsgi/sounding`: the newest of every 6-hourly time over the last 72 h (Thessaloniki launches at 00Z, sometimes 06Z, and ~1 day in 10 has no sounding anywhere; NOAA IGRA2 has the same gaps). It deliberately doesn't use `siphon`: siphon < 0.11 calls the retired cgi-bin interface, and 0.11 needs Python ≥ 3.11 | `upper_air_soundings/<station>_sounding_<YYYYmmdd_HHMM>.png`, never redrawn once it exists. Every run checks Thessaloniki first. When its newest observed sounding is over 36 h old (`FALLBACK_AFTER_HOURS`; HNMS stopped reporting on 2026-09-29), the script also plots the GFS model profile for Thessaloniki from Open-Meteo (`16622_model_*.png`) and the newest observed Sofia sounding (`15614_sounding_*.png`). The dashboard's Upper Air Sounding tab applies the same 36-h rule (`SOUNDING_FALLBACK_AFTER_HOURS`) and shows either Thessaloniki or the two fallbacks |
| `get_anomaly.py` | ERA5 T850 via `cdsapi` (needs `~/.cdsapirc`), compared against `t850_climatology_1975-2005.nc` | `temp_anomaly_map/*.png` |
| `7-day_forecast.py` | Open-Meteo API | `7-day_forecast/Kalamaria_forecast.png` |
| `nowcast.py` | Gemini (`google.generativeai`), given the latest CSV rows, satellite image, sky-camera frames and Skew-T | `nowcasts/nowcast_*.txt` |
| `download_satellite2.sh` (runs from `~`) | Infoplaza tiles: newest available quarter-hour (~30 min server lag); `satellite_latest/pre-process_image.py` stamps the time, `process_image.py` georeferences it and burns in the coastline (buffered coastline built once) | `satellite_latest/satellite_greece.jpg` → `assets/latest_satellite.png`, plus an animation frame `satellite_latest/history/<YYYYmmddHHMM>.jpg` (1600 px, newest 12 kept; the script passes the image time to `process_image.py`). The dashboard's Satellite tab loops the newest 5 frames |
| `camera_capture.sh` (runs from `~`) | `libcamera-still` every 5 min, 04–18 UTC | `whole_sky_camera/*.jpg` (keeps the 30 newest; files named `THUNDER*` are never pruned) |

## Secrets

The Telegram credentials (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) come from `.env` through `python-dotenv`, and `.env` is gitignored. Load new secrets the same way. `nowcast.py` and `timeseries_plotly.py` still hardcode API keys or tokens, so strip those before committing either file.

## Conventions

- Paths are mostly hard-coded absolute paths under `/home/dimitris/weather_station`. Keep new scripts consistent with that, or make them independent of cwd, because cron and the launcher use different working directories.
- Long-running scripts are kept alive by wrapper `while true` loops (`auto_*.sh`, `auto_plotly.py`), not by systemd. Some log messages in those wrappers are in Greek.
- Dashboard static files (PWA manifest, service worker, CSS, background) live in `assets/`, which Dash serves automatically.
