import os, glob, gzip, io, math, sqlite3, threading, time, urllib.parse
from functools import lru_cache
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

import dash
from dash import dcc, html, Input, Output, State, ctx
from dash.exceptions import PreventUpdate
import flask
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo  # Python 3.9+
import ephem
from PIL import Image

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN not found. Make sure it's set in your .env file.")

# Include Roboto font from Google Fonts
external_stylesheets = ['https://fonts.googleapis.com/css?family=Roboto:400,700']
# suppress_callback_exceptions: the satellite animation's components only exist while its tab is shown
app = dash.Dash(__name__, external_stylesheets=external_stylesheets, suppress_callback_exceptions=True)
server = app.server

app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        <link rel="manifest" href="/assets/manifest.json">
        {%css%}
        <style>
            /* Keyframes for blinking effect */
            @keyframes blinker {
                50% { opacity: 0; }
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "weather_station_data.csv")

DATA_WINDOW = timedelta(hours=24)
TAIL_BYTES = 800_000       # ~3 days of rows at ~5,500 rows/day: covers 24 h even with out-of-order rows
MIN_RELOAD_S = 10          # re-read the CSV at most this often, and only when it has changed
REFRESH_MS = 30_000        # browsers poll this often; a tab only re-renders when its data/image changed
MAX_IMAGE_WIDTH = 1600     # images are scaled down to this width before being sent

# Show the BME280 temperature next to the MCP9808 (chart and summary table). It was hidden from
# mid-2026 while the old BME280 read several °C high; the new sensor fitted 2026-11-08 works.
SHOW_BME280_TEMPERATURE = True
# The Gemini nowcast tab is hidden for now; set to True to bring it back.
SHOW_NOWCAST_TAB = False

COL_T_MCP = "Temperature (MCP9808) (°C)"
COL_T_BME = "Temperature (BME280) (°C)"
COL_RH = "Humidity (BME280) (%)"
COL_P = "Pressure (BME280) (hPa)"
COL_LUX = "Light Intensity (BH1750) (lux)"
COL_UV = "UV Index (GY-8511)"
COL_WS = "Wind Speed (Anemometer) (m/s)"
COL_WD = "Wind Direction (Wind Vane) (deg)"
COL_PM1 = "PM1.0 (PMSA003I) (µg/m³)"
COL_PM25 = "PM2.5 (PMSA003I) (µg/m³)"
COL_PM10 = "PM10.0 (PMSA003I) (µg/m³)"
COL_RAIN = "Rain Accumulation (SEN0575) (mm)"
COL_RAIN_EVENT = "Rain Event (LM393)"
COL_LIGHTNING = "Lightning Detection (AS3935)"
COL_LIGHTNING_DIST = "Lightning Distance (AS3935) (km)"
TEXT_COLS = [COL_LIGHTNING, COL_RAIN_EVENT]

# ----------------------------------------------------------------------------
# Compression: gzip text responses (page, layout, callback JSON, Dash's JS bundles).
# plotly.js alone is ~3.5 MB, ~1 MB gzipped. Static bundles are compressed once and cached.
# ----------------------------------------------------------------------------
_COMPRESSIBLE = {"text/html", "text/css", "text/plain", "text/javascript",
                 "application/javascript", "application/json"}
_gzip_cache = {}


@server.after_request
def gzip_response(response):
    request = flask.request
    if ("gzip" not in request.headers.get("Accept-Encoding", "").lower()
            or response.status_code != 200
            or "Content-Encoding" in response.headers
            or response.mimetype not in _COMPRESSIBLE):
        return response
    response.direct_passthrough = False
    data = response.get_data()
    if len(data) < 1024:
        return response
    static = request.method == "GET" and request.path.startswith(("/_dash-component-suites/", "/assets/"))
    key = (request.full_path, len(data)) if static else None
    body = _gzip_cache.get(key) if key else None
    if body is None:
        body = gzip.compress(data, compresslevel=6)
        if key:
            _gzip_cache[key] = body
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(body))
    response.vary.add("Accept-Encoding")
    return response


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
_data_cache = {"key": None, "checked": 0.0, "df": pd.DataFrame()}
# Callbacks run in parallel threads (e.g. the tab and the status bar on page load): the lock makes a
# request that arrives during a reload wait for it, instead of getting the old or empty data.
_data_lock = threading.Lock()


def _read_recent_rows(size):
    """The last DATA_WINDOW of rows. Writers append sparse rows, only roughly in time order."""
    with open(CSV_PATH, "rb") as f:
        header = f.readline()
        start = max(len(header), size - TAIL_BYTES)
        f.seek(start)
        tail = f.read()
    if start > len(header):
        tail = tail[tail.find(b"\n") + 1:]  # drop the partial first line
    text = (header + tail).replace(b"\x00", b"").decode("utf-8", errors="ignore")
    df = pd.read_csv(io.StringIO(text), low_memory=False, on_bad_lines="skip")

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M:%S", errors="coerce", utc=True)
    now_utc = pd.Timestamp.now(tz="UTC")
    # Keep the last 24 h; this also drops future NTP glitches and boards without a set clock (year 2000)
    df = df[(df["timestamp"] >= now_utc - DATA_WINDOW) & (df["timestamp"] <= now_utc + pd.Timedelta(minutes=5))]
    # Sorting makes Plotly draw strictly left-to-right (no zig-zag from out-of-order rows)
    df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)

    numeric_cols = [c for c in df.columns if c != "timestamp" and c not in TEXT_COLS]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    return drop_zero_glitches(df)


# The temperature board's I2C bus glitches make the MCP9808 read exactly 0.00 (12 times from 2025 to
# 2026-11-19, each straight after 11-28 °C). The USB logger leaves these out since 2026-11-19 12:13 local;
# this hides the older ones (and any that slip through): a reading of exactly 0 is dropped when the
# sensor's last non-zero reading is at least ZERO_GLITCH_JUMP from 0, so genuine 0 °C readings stay.
ZERO_GLITCH_JUMP = 2.0
ZERO_GLITCH_COLS = [COL_T_MCP, COL_T_BME]


def drop_zero_glitches(df):
    """df sorted by time; blanks the glitch readings in place and returns df."""
    for col in ZERO_GLITCH_COLS:
        if col not in df.columns:
            continue
        readings = df[col].dropna()
        last_nonzero = readings.where(readings != 0).ffill().shift(1)
        glitches = readings.index[(readings == 0) & (last_nonzero.abs() >= ZERO_GLITCH_JUMP)]
        if len(glitches):
            df.loc[glitches, col] = np.nan
    return df


def load_data():
    with _data_lock:
        now = time.monotonic()
        if _data_cache["key"] is not None and now - _data_cache["checked"] < MIN_RELOAD_S:
            return _data_cache["df"]
        try:
            st = os.stat(CSV_PATH)
        except FileNotFoundError:
            print(f"Error: {CSV_PATH} not found.")
            return _data_cache["df"]
        key = (st.st_mtime_ns, st.st_size)
        if key != _data_cache["key"]:
            try:
                _data_cache["df"] = _read_recent_rows(st.st_size)
                _data_cache["key"] = key
            except Exception as e:
                print(f"Error loading data: {e}")  # keep showing the last good data
        _data_cache["checked"] = now
        return _data_cache["df"]


def series(df, col, freq=None, how="mean"):
    """One sensor's readings (without other writers' empty rows), optionally averaged per time bin."""
    if df.empty or col not in df.columns:
        return pd.Series(dtype=float)
    s = df.loc[df[col].notna(), ["timestamp", col]].set_index("timestamp")[col]
    if freq and not s.empty:
        s = s.resample(freq).agg(how)  # empty bins stay NaN, so lines break where data is missing
    return s


def with_gaps(s, max_gap):
    """Insert a break wherever readings are further apart than max_gap, so lines don't bridge outages."""
    if len(s) < 2:
        return s
    gaps = s.index.to_series().diff() > max_gap
    if not gaps.any():
        return s
    breaks = pd.Series(np.nan, index=s.index[gaps] - pd.Timedelta(seconds=1))
    return pd.concat([s, breaks]).sort_index()


# ----------------------------------------------------------------------------
# Images: served once per new file, scaled down, with long browser caching
# ----------------------------------------------------------------------------
IMAGE_SOURCES = {
    # name: (glob pattern relative to BASE_DIR, output format)
    "cloud-camera": ("whole_sky_camera/*.jpg", "JPEG"),
    "satellite": ("assets/latest_satellite.png", "JPEG"),
    "anomaly": ("temp_anomaly_map/*.png", "PNG"),
    "sounding": ("upper_air_soundings/16622_sounding_*.png", "PNG"),        # observed, Thessaloniki airport
    "sounding-model": ("upper_air_soundings/16622_model_*.png", "PNG"),     # GFS profile, Thessaloniki airport
    "sounding-sofia": ("upper_air_soundings/15614_sounding_*.png", "PNG"),  # observed, Sofia (fallback)
    "sounding-athens": ("upper_air_soundings/16716_sounding_*.png", "PNG"), # observed, Athens Hellinikon (12Z)
    "sounding-larissa": ("upper_air_soundings/16648_model_*.png", "PNG"),   # GFS profile, Larissa (no radiosonde)
    "forecast": ("7-day_forecast/Kalamaria_forecast.png", "PNG"),
}
_image_cache = {}


def latest_image(name):
    files = glob.glob(os.path.join(BASE_DIR, IMAGE_SOURCES[name][0]))
    if not files:
        return None, 0
    path = max(files, key=os.path.getmtime)
    return path, os.path.getmtime(path)


def _render_image(path, fmt):
    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", img.size, "white")
            flat.paste(rgba, mask=rgba.split()[-1])
        else:
            flat = img.convert("RGB")
    flat.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_WIDTH * 3))
    buf = io.BytesIO()
    if fmt == "JPEG":
        flat.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    else:
        flat.save(buf, "PNG", optimize=True)
    return buf.getvalue()


@server.route("/media/<name>")
def media(name):
    if name not in IMAGE_SOURCES:
        flask.abort(404)
    path, mtime = latest_image(name)
    if not path:
        flask.abort(404)
    fmt = IMAGE_SOURCES[name][1]
    cached = _image_cache.get(name)
    if not cached or cached[0] != (path, mtime):
        try:
            cached = ((path, mtime), _render_image(path, fmt))
        except Exception as e:  # e.g. the file is still being written
            print(f"Image {name} not ready: {e}")
            flask.abort(503)
        _image_cache[name] = cached
    resp = flask.Response(cached[1], mimetype="image/jpeg" if fmt == "JPEG" else "image/png")
    # The page links to /media/<name>?v=<file time>, so a new image gets a new URL
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def image_tab(name, alt, missing_text):
    path, mtime = latest_image(name)
    if not path:
        return html.Div(missing_text, style={"textAlign": "center", "padding": "20px"})
    stamp = datetime.fromtimestamp(mtime, ZoneInfo("Europe/Athens")).strftime("%d %b %Y, %H:%M")
    return html.Div(
        [
            html.Img(src=f"/media/{name}?v={int(mtime)}", alt=alt, style={"maxWidth": "100%", "height": "auto"}),
            html.Div(f"Updated {stamp} (Athens time)", style={"fontSize": "13px", "color": "#666", "marginTop": "6px"}),
        ],
        style={"textAlign": "center", "padding": "10px"}
    )


# Without an observed Thessaloniki sounding for this long, show the fallbacks instead
# (the same rule as FALLBACK_AFTER_HOURS in plot_skewt.py, which then plots them).
SOUNDING_FALLBACK_AFTER_HOURS = 36


def sounding_valid_time(path):
    """Valid time from the plot's file name: <station>_<sounding|model>_<YYYYmmdd_HHMM>.png"""
    try:
        return datetime.strptime(os.path.basename(path)[-17:-4], "%Y%m%d_%H%M").replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return None


def sounding_figure(name, caption):
    path, mtime = latest_image(name)
    if not path:
        return None
    valid = sounding_valid_time(path)
    when = f"valid {valid:%d %b %Y %H:%M} UTC" if valid else ""
    return html.Div(
        [
            html.Img(src=f"/media/{name}?v={int(mtime)}", alt=f"{caption} {when}", style={"maxWidth": "100%", "height": "auto"}),
            html.Div(f"{caption}, {when}", style={"fontSize": "13px", "color": "#666", "marginTop": "6px"}),
        ],
        style={"flex": "1 1 420px", "minWidth": "300px", "textAlign": "center", "padding": "6px"}
    )


def sounding_tab():
    """The observed Thessaloniki sounding while it is current (otherwise the GFS profile and Sofia), then
    the GFS profile for Larissa (no radiosonde there) and the newest observed Athens sounding."""
    larissa = sounding_figure("sounding-larissa", "GFS model profile, Larissa (not observed)")
    athens = sounding_figure("sounding-athens", "Observed sounding, Athens Hellinikon (16716)")
    obs_path, _ = latest_image("sounding")
    obs_valid = sounding_valid_time(obs_path) if obs_path else None
    now_utc = datetime.now(ZoneInfo("UTC"))
    if obs_valid and now_utc - obs_valid <= timedelta(hours=SOUNDING_FALLBACK_AFTER_HOURS):
        figures = [f for f in (sounding_figure("sounding", "Observed sounding, Thessaloniki airport (16622)"),
                               larissa, athens) if f is not None]
        return html.Div(figures, style={"display": "flex", "flexWrap": "wrap", "justifyContent": "center"})

    since = f"since {obs_valid:%d %b %Y %H}Z" if obs_valid else "for now"
    note = html.Div(
        f"No observed sounding from Thessaloniki airport {since}. Showing the GFS model profile for "
        "Thessaloniki and the latest observed sounding from Sofia, 239 km north.",
        style={"textAlign": "center", "fontSize": "14px", "padding": "8px 12px", "margin": "6px auto",
               "maxWidth": "900px", "backgroundColor": "rgba(255, 243, 205, 0.9)", "border": "1px solid #e0c97f",
               "borderRadius": "6px"}
    )
    figures = [f for f in (sounding_figure("sounding-model", "GFS model profile, Thessaloniki airport (not observed)"),
                           sounding_figure("sounding-sofia", "Observed sounding, Sofia (15614)"), larissa, athens)
               if f is not None]
    if not figures:
        figures = [html.Div("No sounding available at the moment.", style={"textAlign": "center", "padding": "20px"})]
    return html.Div([note, html.Div(figures, style={"display": "flex", "flexWrap": "wrap", "justifyContent": "center"})])


def get_latest_nowcast_text():
    folder_path = os.path.join(BASE_DIR, "nowcasts/*")
    list_of_files = glob.glob(folder_path)
    if not list_of_files:
        return "No nowcast available at the moment."
    try:
        latest_file = max(list_of_files, key=os.path.getctime)
        with open(latest_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except Exception as e:
        print(f"Error reading nowcast file: {e}")
        return "Error loading the nowcast content."


# ----------------------------------------------------------------------------
# Figures
# ----------------------------------------------------------------------------
FONT_FAMILY = "Roboto, sans-serif"
title_font = dict(size=20, family=FONT_FAMILY)
tick_font = dict(size=13, family=FONT_FAMILY)
legend_font = dict(size=14, family=FONT_FAMILY)
GRAPH_HEIGHT = 450
GRAPH_CONFIG = {"displayModeBar": False, "responsive": True}


def graph(fig, height=GRAPH_HEIGHT):
    return dcc.Graph(figure=fig, config=GRAPH_CONFIG, style={"height": f"{height}px"}, className="dash-graph")


def base_figure(title, ytitle, show_legend=False):
    now_utc = pd.Timestamp.now(tz="UTC")
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        title=dict(text=title, x=0.02, xanchor="left", font=title_font),
        font=dict(family=FONT_FAMILY, size=13, color="#333"),
        height=GRAPH_HEIGHT,
        margin=dict(l=60, r=20, t=55, b=45),
        hovermode="x unified",
        dragmode=False,
        showlegend=show_legend,
        legend=dict(orientation="h", font=legend_font, x=0.5, xanchor="center", y=-0.15, yanchor="top"),
        paper_bgcolor="rgba(255,255,255,0.92)",
    )
    fig.update_xaxes(
        type="date", range=[now_utc - DATA_WINDOW, now_utc], tickformat="%H:%M", hoverformat="%d %b %H:%M UTC",
        title=dict(text="Time (UTC)"), tickfont=tick_font, showline=True, linecolor="#999", gridcolor="#eee",
    )
    fig.update_yaxes(title=dict(text=ytitle), tickfont=tick_font, showline=True, linecolor="#999",
                     gridcolor="#eee", zeroline=False)
    return fig


def no_data(fig, text="No data in the last 24 hours"):
    fig.add_annotation(text=text, xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
                       font=dict(size=16, color="#888"))
    return fig


def message_figure(text, height=380):
    """A blank figure with just a message (for the gauge, compass and roses, which have no time axis)."""
    fig = no_data(go.Figure(), text)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(template="plotly_white", height=height, paper_bgcolor="rgba(255,255,255,0.92)")
    return fig


def add_line(fig, s, name, color, unit, decimals=1, **kwargs):
    fig.add_trace(go.Scatter(
        x=s.index, y=s.round(decimals).values, mode="lines", name=name,
        line=dict(color=color, width=2), hovertemplate=f"%{{y:.{decimals}f}} {unit}<extra>{name}</extra>",
        **kwargs,
    ))


# ----------------------------------------------------------------------------
# Climatology: the 1991-2020 normal for Kalamaria (built by build_climatology.py), drawn behind the charts
# ----------------------------------------------------------------------------
SHOW_CLIMATOLOGY = True
CLIM_DIR = os.path.join(BASE_DIR, "climatology")
CLIM_PERIOD = "1991–2020"
CLIM_SOURCES = {"t": "ERA5-Land", "rh": "ERA5-Land", "p": "ERA5, sea level", "ws": "ERA5, 10 m",
                "lux": "ERA5 sunlight × 60 lux per W/m²", "uv": "CAMS"}
CLIM_LINE = dict(color="rgba(80, 80, 80, 0.8)", width=1.6, dash="dash")
CLIM_BAND = "rgba(120, 120, 120, 0.15)"


@lru_cache(maxsize=1)
def _climatology(mtime):
    """{var: (hourly array (366*24, 3), daily array (366, 3))} of mean, p10, p90; reloaded when the file changes."""
    hourly = pd.read_csv(os.path.join(CLIM_DIR, "kalamaria_hourly.csv"))
    daily = pd.read_csv(os.path.join(CLIM_DIR, "kalamaria_daily.csv"))
    out = {}
    for c in hourly.columns:
        if c.endswith("_mean"):
            v = c[:-len("_mean")]
            cols = [f"{v}_mean", f"{v}_p10", f"{v}_p90"]
            out[v] = (hourly[cols].to_numpy(float), daily[cols].to_numpy(float))
    return out


def climatology_tables():
    if not SHOW_CLIMATOLOGY:
        return {}
    try:
        return _climatology(os.path.getmtime(os.path.join(CLIM_DIR, "kalamaria_hourly.csv")))
    except Exception as e:
        print(f"Climatology unavailable: {type(e).__name__}: {e}")
        return {}


def _leap_doy0(idx):
    """0-365: a date's position in a leap year, as in build_climatology.py (1 March is always 60)."""
    doy = idx.dayofyear.to_numpy() - 1
    return np.where(~idx.is_leap_year & (doy >= 59), doy + 1, doy)


def _epoch_times(start_s, end_s, step_s):
    return pd.to_datetime(np.arange(start_s, end_s, step_s), unit="s", utc=True)


def climatology_at(var, times):
    """The hourly normal (columns mean, p10, p90) at the UTC `times`, interpolated between the hours."""
    table = climatology_tables().get(var)
    if table is None or len(times) == 0:
        return None
    hourly = table[0]
    pos = _leap_doy0(times) * 24 + times.hour.to_numpy() + (times.minute.to_numpy() + times.second.to_numpy() / 60) / 60
    i = np.floor(pos).astype(int)
    f = (pos - i)[:, None]
    values = hourly[i % len(hourly)] * (1 - f) + hourly[(i + 1) % len(hourly)] * f
    return pd.DataFrame(values, index=times, columns=["mean", "p10", "p90"])


CLIM_SMOOTH_FROM = 3 * 3600   # history bins at least this long get the seasonal (daily) normal, not the daily cycle


def climatology_binned(var, start_s, end_s, b):
    """The normal for a history chart of `b`-second bins (UTC index):
    - bins under CLIM_SMOOTH_FROM: the hourly normal averaged over each bin (index: bin start, as the data);
    - longer bins under a day: one point per day at noon, the normal daily mean with the range of the day's
      hourly values (lowest 10th to highest 90th percentile), since drawing the daily cycle every few pixels
      over months turns it into a smear;
    - daily and longer bins: the normal of daily means (and its percentiles), averaged per bin."""
    table = climatology_tables().get(var)
    if table is None:
        return None
    if b < CLIM_SMOOTH_FROM:
        step = min(b, 3600)
        c = climatology_at(var, _epoch_times(start_s + step / 2, end_s, step))   # the middle of each step
        return c.resample(f"{b}s", origin="epoch").mean().dropna()
    days = _epoch_times(start_s // 86400 * 86400, end_s, 86400)
    d = _leap_doy0(days)
    if b < 86400:
        hourly = table[0].reshape(366, 24, 3)
        values = np.column_stack([table[1][d, 0], hourly[d, :, 1].min(axis=1), hourly[d, :, 2].max(axis=1)])
        return pd.DataFrame(values, index=days + pd.Timedelta(hours=12), columns=["mean", "p10", "p90"])
    c = pd.DataFrame(table[1][d], index=days, columns=["mean", "p10", "p90"])
    return c.resample(f"{b}s", origin="epoch").mean().dropna()


def climatology_note(b):
    """How the History tab's normal is drawn at bin size `b`."""
    how = ("averaged the same way, with the range of 80 % of the hourly values (10th to 90th percentile)"
           if b < CLIM_SMOOTH_FROM else
           "as its daily mean, with the range of 80 % of the hourly values through the day (from the lowest 10th "
           "to the highest 90th percentile)" if b < 86400 else
           "as daily means, with the range of 80 % of the daily means (10th to 90th percentile)")
    return (f" Grey: the {CLIM_PERIOD} normal for Kalamaria (dashed), {how}. Temperature and humidity from "
            "ERA5-Land; sea-level pressure, 10 m wind and light from ERA5 (light: sunlight × 60 lux per W/m², "
            "this sensor's ratio).")


def add_climatology(fig, var, unit, decimals=1, c=None, row=None, col=None, showlegend=True):
    """Grey 10-90 % band and dashed mean of the normal. Without `c`, over the 24-hour axis of base_figure, moved
    under the station's lines. Returns the normal drawn (or None)."""
    on_24h_axis = c is None
    if on_24h_axis:
        end = time.time()
        c = climatology_at(var, _epoch_times((end - DATA_WINDOW.total_seconds()) // 600 * 600, end + 600, 600))
    if c is None or c.empty:
        return None
    n = len(fig.data)
    where = dict(row=row, col=col) if row else {}
    name = f"Normal {CLIM_PERIOD}"
    rng = c[["p10", "p90"]].round(decimals).values
    fig.add_trace(go.Scatter(x=c.index, y=c["p90"].round(decimals).values, mode="lines", line=dict(width=0),
                             hoverinfo="skip", showlegend=False, legendgroup="clim"), **where)
    fig.add_trace(go.Scatter(x=c.index, y=c["p10"].round(decimals).values, mode="lines", line=dict(width=0),
                             fill="tonexty", fillcolor=CLIM_BAND, name="Normal range (10–90 %)", hoverinfo="skip",
                             legendgroup="clim", showlegend=showlegend), **where)
    fig.add_trace(go.Scatter(
        x=c.index, y=c["mean"].round(decimals).values, mode="lines", line=CLIM_LINE, name=name, customdata=rng,
        legendgroup="clim", showlegend=showlegend,
        hovertemplate=(f"%{{y:.{decimals}f}} {unit} (10–90 %: %{{customdata[0]:.{decimals}f}} to "
                       f"%{{customdata[1]:.{decimals}f}})<extra>{name} ({CLIM_SOURCES.get(var, '')})</extra>")), **where)
    if on_24h_axis:
        fig.data = fig.data[n:] + fig.data[:n]   # behind the station's lines
        fig.update_layout(showlegend=True)
    return c


def create_line_figure(df, col, title, ytitle, unit, color, decimals=1, max_gap=timedelta(minutes=20), clim=None,
                       **kwargs):
    fig = base_figure(title, ytitle)
    s = series(df, col)
    if not s.empty:
        add_line(fig, with_gaps(s, max_gap), "Station", color, unit, decimals, **kwargs)
    if clim:
        add_climatology(fig, clim, unit, decimals)
    if s.empty:
        return no_data(fig)
    return fig


def create_temperature_figure(df):
    fig = base_figure("Temperature", "Temperature (°C)", show_legend=SHOW_BME280_TEMPERATURE)
    s = series(df, COL_T_MCP)
    add_line(fig, with_gaps(s, timedelta(minutes=20)), "MCP9808", "#d62728", "°C")
    bme = series(df, COL_T_BME) if SHOW_BME280_TEMPERATURE else s.iloc[:0]
    if SHOW_BME280_TEMPERATURE:
        add_line(fig, with_gaps(bme, timedelta(minutes=20)), "BME280", "#1f77b4", "°C")
    add_climatology(fig, "t", "°C")
    if s.empty and bme.empty:
        return no_data(fig)
    return fig


def create_humidity_figure(df):
    fig = create_line_figure(df, COL_RH, "Relative Humidity", "Relative humidity (%)", "%", "#17a2b8", decimals=0,
                             clim="rh")
    fig.update_yaxes(range=[0, 100])
    return fig


def create_pressure_figure(df):
    fig = create_line_figure(df, COL_P, "Atmospheric Pressure", "Pressure (hPa)", "hPa", "#6f42c1", clim="p")
    fig.update_yaxes(tickformat=".1f")  # "1023.2", not "1,023.2"
    return fig


def create_light_figure(df):
    return create_line_figure(df, COL_LUX, "Ambient Visible Light", "Illuminance (lux)", "lux", "#e0a100",
                              decimals=0, clim="lux", fill="tozeroy", fillcolor="rgba(255, 193, 7, 0.25)")


# WHO UV index categories
UV_BANDS = [(0, 3, "Low", "rgba(76, 175, 80, 0.14)"),
            (3, 6, "Moderate", "rgba(255, 235, 59, 0.25)"),
            (6, 8, "High", "rgba(255, 152, 0, 0.20)"),
            (8, 11, "Very high", "rgba(244, 67, 54, 0.16)"),
            (11, 20, "Extreme", "rgba(156, 39, 176, 0.16)")]


def create_uv_figure(df):
    fig = create_line_figure(df, COL_UV, "UV Index", "UV index", "", "#6a1b9a", clim="uv")
    ys = [np.asarray(t.y, dtype=float) for t in fig.data if t.y is not None and len(t.y)]
    top = max([0.0] + [float(y[np.isfinite(y)].max()) for y in ys if np.isfinite(y).any()])   # incl. the normal
    ymax = max(6.0, math.ceil(top + 1))
    for lo, hi, label, color in UV_BANDS:
        if lo < ymax:
            fig.add_hrect(y0=lo, y1=min(hi, ymax), fillcolor=color, line_width=0, layer="below",
                          annotation_text=label, annotation_position="top left",
                          annotation_font=dict(size=11, color="#666"))
    fig.update_yaxes(range=[0, ymax])
    return fig


def create_wind_timeseries_figure(df):
    """10-minute mean and maximum wind speed, with direction arrows every 30 minutes."""
    fig = base_figure("Wind Speed and Direction (10-minute averages)", "Wind speed (m/s)", show_legend=True)
    speed = series(df, COL_WS)
    if speed.empty:
        return no_data(fig, "Wind data not available")
    # Break the lines only for real outages (> 30 min), not for a single empty 10-minute bin
    mean10 = with_gaps(speed.resample("10min").mean().dropna(), timedelta(minutes=30))
    max10 = with_gaps(speed.resample("10min").max().dropna(), timedelta(minutes=30))
    fig.add_trace(go.Scatter(x=max10.index, y=max10.round(1).values, mode="lines", name="10-min maximum",
                             line=dict(color="rgba(65, 105, 225, 0.45)", width=1, dash="dot"),
                             hovertemplate="%{y:.1f} m/s<extra>max</extra>"))
    fig.add_trace(go.Scatter(x=mean10.index, y=mean10.round(1).values, mode="lines", name="10-min mean",
                             line=dict(color="royalblue", width=2.5),
                             hovertemplate="%{y:.1f} m/s<extra>mean</extra>"))

    direction = series(df, COL_WD)
    if not direction.empty:
        # Vector mean of the direction the wind comes from, per 30 minutes
        rad = np.deg2rad(direction)
        u = np.sin(rad).resample("30min").mean()
        v = np.cos(rad).resample("30min").mean()
        from_deg = (np.rad2deg(np.arctan2(u, v)) % 360).dropna()
        # The arrows point where the wind blows to (from + 180°), as before
        fig.add_trace(go.Scatter(
            x=from_deg.index + pd.Timedelta(minutes=15), y=[1] * len(from_deg), mode="markers",
            name="Direction", yaxis="y2", hoverinfo="skip",
            marker=dict(symbol="arrow", size=15, angle=((from_deg + 180) % 360).values, angleref="up",
                        color="rgba(200, 30, 30, 0.85)", line=dict(width=1, color="DarkSlateGrey")),
        ))
        fig.update_layout(yaxis2=dict(visible=False, range=[0, 1.12], overlaying="y", fixedrange=True))
    add_climatology(fig, "ws", "m/s")
    fig.update_layout(yaxis=dict(rangemode="tozero"))
    return fig


def create_air_quality_figure(df):
    fig = base_figure("Air Quality (5-minute averages)", "Concentration (µg/m³)", show_legend=True)
    # PM10 first as a wide pale band, so PM2.5 stays visible on top when the two are equal
    traces = [(COL_PM10, "PM10.0", "rgba(0, 128, 0, 0.35)", 7), (COL_PM25, "PM2.5", "#ff7f0e", 2),
              (COL_PM1, "PM1.0", "#1f77b4", 2)]
    has_data = False
    for col, name, color, width in traces:
        s = series(df, col, freq="5min").dropna()
        if not s.empty:
            has_data = True
            add_line(fig, with_gaps(s, timedelta(minutes=15)), name, color, "µg/m³")
            fig.data[-1].line.width = width
    if not has_data:
        return no_data(fig)
    for pollutant, thresh in {"PM1.0": 20, "PM2.5": 25, "PM10.0": 50}.items():
        fig.add_hline(y=thresh, line=dict(dash="dash", width=1.5, color="#444"),
                      annotation_text=f"{pollutant} {thresh} µg/m³", annotation_position="top right",
                      annotation_font=dict(size=11, color="#444"))
    fig.update_yaxes(rangemode="tozero")
    return fig


def create_rain_figure(df):
    fig = base_figure("Rain Accumulation (last 24 hours)", "Precipitation (mm)")
    s = series(df, COL_RAIN)
    if s.empty:
        no_data(fig, "No rain recorded in the last 24 hours")
        fig.update_yaxes(range=[0, 1])
        return fig
    # The gauge reports its running total; hold the last value up to now
    s = pd.concat([s, pd.Series([s.iloc[-1]], index=[pd.Timestamp.now(tz="UTC")])])
    add_line(fig, s, "Rain", "#1565c0", "mm", line_shape="hv", fill="tozeroy", fillcolor="rgba(21, 101, 192, 0.18)")
    fig.update_yaxes(rangemode="tozero")
    return fig


# 16 directions: the vane reports 8 positions 45° apart, and since the +23° correction in wind.py
# (2026-10-31) they sit at 23°, 68°, 113°... With 8 bins each value would fall just past a bin
# edge (22.5°, 67.5°...) and land one bin clockwise; with 16 bins it falls in the middle of one.
WIND_CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
WIND_SPEED_BINS = [0, 1, 2, 3, 4, 5, 6, 7, 10, 15, 25]
WIND_SPEED_LABELS = ["0-1", "1-2", "2-3", "3-4", "4-5", "5-6", "6-7", "7-10", "10-15", "15+"]
WIND_SPEED_COLORS = px.colors.sequential.Plasma_r


def create_wind_rose(df):
    if df.empty or COL_WD not in df.columns:
        return go.Figure()
    try:
        wind_df = df[[COL_WD, COL_WS]].dropna().copy()
        if wind_df.empty:
            return message_figure("No wind data", 400)
        wind_df["deg"] = wind_df[COL_WD] % 360
        wind_df["cardinal"] = [WIND_CARDINALS[int(((d + 11.25) % 360) // 22.5)] for d in wind_df["deg"]]
        # Calms (0 m/s) fall outside the first bin and are left out: they have no meaningful direction
        wind_df["strength"] = pd.cut(wind_df[COL_WS], bins=WIND_SPEED_BINS, labels=WIND_SPEED_LABELS)

        # Built directly with Barpolar: plotly.express took ~1 s per rose for the same chart
        counts = (pd.crosstab(wind_df["strength"], wind_df["cardinal"])
                  .reindex(index=WIND_SPEED_LABELS, columns=WIND_CARDINALS, fill_value=0))
        fig = go.Figure()
        for i, label in enumerate(WIND_SPEED_LABELS):
            fig.add_trace(go.Barpolar(r=counts.loc[label].values, theta=WIND_CARDINALS, name=label,
                                      marker_color=WIND_SPEED_COLORS[i % len(WIND_SPEED_COLORS)],
                                      hovertemplate=f"{label} m/s: %{{r}}<extra>%{{theta}}</extra>"))
        fig.update_layout(
            template="plotly_white",
            showlegend=False,  # one shared HTML colour key sits under the three roses
            polar=dict(
                angularaxis=dict(direction="clockwise", rotation=90, tickfont=tick_font, gridcolor="#eee",
                                 categoryorder="array", categoryarray=WIND_CARDINALS),
                radialaxis=dict(visible=False),
                bgcolor="whitesmoke"
            ),
            title=dict(text="Wind Rose", font=title_font),
            height=400,
            margin=dict(t=70, b=40, l=40, r=40),
            font=dict(family=FONT_FAMILY),
            paper_bgcolor="rgba(255,255,255,0.92)",
        )
        return fig
    except Exception as e:
        print(f"Wind rose error: {e}")
        return go.Figure()


def create_wind_rose_range(df, time_delta, title_text):
    now_utc = datetime.now(ZoneInfo("UTC"))
    filtered_df = df[df["timestamp"] >= now_utc - time_delta] if not df.empty else df
    fig = create_wind_rose(filtered_df)
    fig.update_layout(title=dict(text=title_text, font=title_font))
    return fig


def wind_speed_key():
    swatch = {"display": "inline-block", "width": "12px", "height": "12px", "marginRight": "4px", "verticalAlign": "middle"}
    return html.Div(
        [html.Span("Wind speed (m/s):", style={"fontWeight": "bold", "marginRight": "10px"})]
        + [html.Span([html.Span(style={**swatch, "backgroundColor": WIND_SPEED_COLORS[i % len(WIND_SPEED_COLORS)]}), label],
                     style={"marginRight": "12px", "whiteSpace": "nowrap"})
           for i, label in enumerate(WIND_SPEED_LABELS)],
        style={"display": "flex", "flexWrap": "wrap", "justifyContent": "center", "alignItems": "center",
               "rowGap": "6px", "fontSize": "13px", "padding": "8px 10px", "backgroundColor": "rgba(255,255,255,0.92)"}
    )


def get_moon_phase_and_rise_info():
    """
    Calculates the current moon phase, name, icon, illumination,
    and next moonrise time for Thessaloniki.
    """
    current_dt_utc = datetime.now(ZoneInfo("UTC"))
    obs_date = ephem.Date(current_dt_utc)

    moon = ephem.Moon()
    moon.compute(obs_date)

    illumination_percent = moon.moon_phase * 100

    prev_new_moon_date = ephem.previous_new_moon(obs_date)
    moon_age_days = obs_date - prev_new_moon_date

    synodic_period = 29.530588853
    phase_angle_deg = (moon_age_days / synodic_period) * 360.0

    phase_name = ""
    icon_char = ""

    if phase_angle_deg >= 337.5 or phase_angle_deg < 22.5:
        phase_name = "New Moon"
        icon_char = "🌑"
    elif phase_angle_deg < 67.5:
        phase_name = "Waxing Crescent"
        icon_char = "🌒"
    elif phase_angle_deg < 112.5:
        phase_name = "First Quarter"
        icon_char = "🌓"
    elif phase_angle_deg < 157.5:
        phase_name = "Waxing Gibbous"
        icon_char = "🌔"
    elif phase_angle_deg < 202.5:
        phase_name = "Full Moon"
        icon_char = "🌕"
    elif phase_angle_deg < 247.5:
        phase_name = "Waning Gibbous"
        icon_char = "🌖"
    elif phase_angle_deg < 292.5:
        phase_name = "Last Quarter"
        icon_char = "🌗"
    elif phase_angle_deg < 337.5:
        phase_name = "Waning Crescent"
        icon_char = "🌘"
    else:
        phase_name = "Unknown Phase"
        icon_char = "❔"

    phase_display_str = f"{phase_name} {icon_char} ({illumination_percent:.0f}% illum.)"

    # Moonrise for Thessaloniki
    moonrise_info_str = "Moonrise (Thessaloniki): N/A"
    try:
        thessaloniki_lat = '40.6401'  # Latitude for Thessaloniki
        thessaloniki_lon = '22.9444'  # Longitude for Thessaloniki

        observer = ephem.Observer()
        observer.lat = thessaloniki_lat
        observer.lon = thessaloniki_lon
        observer.date = obs_date # Current UTC for calculation

        next_rise_utc_event = observer.next_rising(ephem.Moon())

        moonrise_utc_dt = next_rise_utc_event.datetime() # naive UTC datetime
        moonrise_local_dt = moonrise_utc_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Athens"))

        current_local_observer_dt = datetime.now(ZoneInfo("Europe/Athens"))

        # Display date if moonrise is not on the current local day
        if moonrise_local_dt.date() == current_local_observer_dt.date():
            moonrise_time_str = moonrise_local_dt.strftime("%H:%M %Z")
        else:
            moonrise_time_str = moonrise_local_dt.strftime("%b %d, %H:%M %Z") # e.g., May 14, 03:30 EEST

        moonrise_info_str = f"Moonrise (Thessaloniki): {moonrise_time_str}"

    except ephem.NeverUpError:
        moonrise_info_str = "Moonrise (Thessaloniki): Does not rise"
    except ephem.AlwaysUpError:
        moonrise_info_str = "Moonrise (Thessaloniki): Always up"
    except Exception as e_rise:
        print(f"Error calculating moonrise for Thessaloniki: {e_rise}")

    return f"{phase_display_str} | {moonrise_info_str}"


def create_dashboard(df):
    if df.empty:
        return html.Div("No data available", style={"textAlign": "center", "fontFamily": "Roboto, sans-serif"})

    # Define the parameters to display in the summary table.
    dashboard_parameters = [
        *([COL_T_BME] if SHOW_BME280_TEMPERATURE else []),
        COL_T_MCP,
        COL_RH,
        COL_P,
        COL_PM1,
        COL_PM25,
        COL_PM10,
        COL_WS,
        COL_UV
    ]

    # Define which parameters get both min and max versus max only.
    extra_stats = {
        COL_T_BME: "minmax",
        COL_T_MCP: "minmax",
        COL_RH: "minmax",
        COL_P: "minmax",
        COL_PM1: "minmax",
        COL_PM25: "minmax",
        COL_PM10: "minmax",
        COL_WS: "max",
        COL_UV: "max"
    }

    # Define thresholds for PM parameters.
    pm_thresholds = {COL_PM1: 20, COL_PM25: 25, COL_PM10: 50}

    cell = {"padding": "8px", "border": "1px solid #ccc", "fontSize": "14px"}
    rows = []
    for param in dashboard_parameters:
        if param in df.columns:
            df_param = df[df[param].notnull()]
            # Determine the display parameter name.
            if param.startswith("UV Index"):
                display_param = "UV Index"
            elif param.startswith("Wind Speed"):
                display_param = "Wind Speed (km/h)"
            else:
                # Keep text before the first parenthesis and add the unit from the last parentheses.
                unit_start = param.rfind("(")
                if unit_start != -1:
                    unit_str = param[unit_start:].strip()  # e.g., "(°C)" or "(µg/m³)"
                    display_param = param.split(" (")[0] + " " + unit_str
                else:
                    display_param = param

            value_style = {}
            if df_param.empty:
                latest_value = "N/A"
                stats_str = ""
            else:
                latest_row = df_param.loc[df_param["timestamp"].idxmax()]
                latest_value = latest_row[param]
                # Special handling for Wind Speed: convert m/s to km/h.
                if param == COL_WS:
                    latest_value = round(latest_value * 3.6, 1)

                if extra_stats.get(param) == "minmax":
                    min_val = df_param[param].min()
                    max_val = df_param[param].max()
                    min_row = df_param[df_param[param] == min_val].iloc[0]
                    max_row = df_param[df_param[param] == max_val].iloc[0]
                    if param == COL_WS:
                        min_val = round(min_val * 3.6, 1)
                        max_val = round(max_val * 3.6, 1)
                    min_ts = min_row["timestamp"].astimezone(ZoneInfo("Europe/Athens")).strftime("%H:%M")
                    max_ts = max_row["timestamp"].astimezone(ZoneInfo("Europe/Athens")).strftime("%H:%M")
                    stats_str = f"Min: {min_val} ({min_ts}), Max: {max_val} ({max_ts})"
                elif extra_stats.get(param) == "max":
                    max_val = df_param[param].max()
                    max_row = df_param[df_param[param] == max_val].iloc[0]
                    if param == COL_WS:
                        max_val = round(max_val * 3.6, 1)
                    max_ts = max_row["timestamp"].astimezone(ZoneInfo("Europe/Athens")).strftime("%H:%M")
                    stats_str = f"Max: {max_val} ({max_ts})"
                else:
                    stats_str = ""

                # For PM parameters, if the latest and maximum values exceed thresholds, apply bold red styling.
                if param in pm_thresholds:
                    threshold = pm_thresholds[param]
                    if latest_value > threshold:
                        max_val = df_param[param].max() if extra_stats.get(param) == "minmax" else None
                        if (max_val is not None and max_val > threshold) or (max_val is None):
                            value_style = {"color": "red", "fontWeight": "bold"}

            rows.append(
                html.Tr([
                    html.Td(display_param, style=cell),
                    html.Td(latest_value, style={**cell, **value_style}),
                    html.Td(stats_str, style={**cell, **value_style})
                ])
            )

    # Daily Rainfall: (max - min) over the loaded 24 hours.
    if COL_RAIN in df.columns and df[COL_RAIN].notna().any():
        daily_rainfall = round(df[COL_RAIN].max() - df[COL_RAIN].min(), 1)
    else:
        daily_rainfall = "N/A"

    # Rain Intensity: if it's currently raining (last "Rain Event (LM393)" value),
    # use the accumulation over the last 60 minutes.
    if COL_RAIN in df.columns and COL_RAIN_EVENT in df.columns:
        events = df[df[COL_RAIN_EVENT].notnull()]
        if not events.empty and str(events.iloc[-1][COL_RAIN_EVENT]).strip().lower() == "rain":
            now_utc = datetime.now(ZoneInfo("UTC"))
            df_last60 = df[df["timestamp"] >= now_utc - timedelta(minutes=60)]
            if df_last60[COL_RAIN].notna().any():
                rain_intensity = round(df_last60[COL_RAIN].max() - df_last60[COL_RAIN].min(), 1)
            else:
                rain_intensity = "N/A"
        else:
            rain_intensity = 0
    else:
        rain_intensity = "N/A"

    rows.append(html.Tr([html.Td("Daily Rainfall (mm)", style=cell), html.Td(daily_rainfall, style=cell), html.Td("", style=cell)]))
    rows.append(html.Tr([html.Td("Rain Intensity (mm/h)", style=cell), html.Td(rain_intensity, style=cell), html.Td("", style=cell)]))

    header_table = html.Thead(
        html.Tr([
            html.Th("Parameter", style=cell),
            html.Th("Latest Value", style=cell),
            html.Th("Min/Max (with Timestamps)", style=cell)
        ])
    )

    table = html.Table(
        [header_table, html.Tbody(rows)],
        style={"width": "100%", "margin": "0 auto", "borderCollapse": "collapse"}
    )

    return html.Div(
        [
            html.H2("Latest Measurements", style={"fontFamily": "Roboto, sans-serif", "fontWeight": "bold", "fontSize": "20px"}),
            table
        ],
        style={
            "padding": "10px",
            "textAlign": "center",
            "backgroundColor": "rgba(255,255,255,0.9)",
            "border": "1px solid #ccc",
            "borderRadius": "10px",
            "width": "100%",
            "maxWidth": "400px",
            "margin": "20px auto",
            "fontFamily": "Roboto, sans-serif"
        }
    )


def create_thermometer_dashboard(df):
    # Use the MCP9808: since mid-2026 the BME280 reads several °C high (heated or failing sensor)
    sensor_col = COL_T_MCP
    if df.empty or sensor_col not in df.columns or df[sensor_col].isnull().all():
        return message_figure("No temperature data")

    # Today (local) from midnight onward
    now_greece = datetime.now(ZoneInfo("Europe/Athens"))
    midnight_utc = now_greece.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    readings = df[(df["timestamp"] >= midnight_utc) & df[sensor_col].notna()]
    if readings.empty:
        return message_figure("No temperature data today")

    latest_value = readings.loc[readings["timestamp"].idxmax()][sensor_col]
    min_value = readings[sensor_col].min()
    max_value = readings[sensor_col].max()

    # Set the gauge range a bit beyond the min and max for aesthetics.
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=latest_value,
        number={"suffix": " °C", "valueformat": ".1f"},
        title={"text": "Temperature"},
        gauge={
            "axis": {"range": [min_value - 1, max_value + 1], "dtick": 1.0},
            "bar": {"color": "red"},
            "steps": [
                {"range": [min_value - 0.125, min_value + 0.125], "color": "lightblue"},
                {"range": [max_value - 0.125, max_value + 0.125], "color": "orange"}
            ],
            "threshold": {"line": {"color": "black", "width": 4}, "thickness": 0.80, "value": latest_value}
        }
    ))
    fig.add_annotation(x=0.5, y=-0.15, xref="paper", yref="paper",
                       text=f"Today — Min: {min_value:.1f} °C, Max: {max_value:.1f} °C",
                       showarrow=False, font=dict(size=14))
    fig.update_layout(height=380, margin=dict(t=50, b=90, l=30, r=30), font=dict(family=FONT_FAMILY),
                      paper_bgcolor="rgba(255,255,255,0.92)")
    return fig


COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def create_wind_direction_figure(mean_wind_speed_kmh, mean_direction):
    fig = go.Figure()
    r = 0.4  # circle radius

    fig.add_shape(type="circle", xref="x", yref="y", x0=0.5 - r, x1=0.5 + r, y0=0.5 - r, y1=0.5 + r,
                  line=dict(color="black", width=2), fillcolor="white")

    # Cardinal directions: N=top, E=right, S=bottom, W=left
    offset = 0.06
    for x, y, t in [(0.5, 0.5 + r + offset, "N"), (0.5 + r + offset, 0.5, "E"),
                    (0.5, 0.5 - r - offset, "S"), (0.5 - r - offset, 0.5, "W")]:
        fig.add_annotation(x=x, y=y, text=t, showarrow=False, font=dict(size=16), xref="x", yref="y")

    # The arrow points where the wind blows to (180° from the meteorological direction)
    angle_rad = math.radians(mean_direction)
    x_dest = 0.5 - r * math.sin(angle_rad)
    y_dest = 0.5 - r * math.cos(angle_rad)
    fig.add_annotation(x=x_dest, y=y_dest, ax=0.5, ay=0.5, xref="x", yref="y", axref="x", ayref="y",
                       arrowhead=5, arrowwidth=5, arrowcolor="red", showarrow=True)

    fig.add_annotation(x=0.5, y=0.5, xref="x", yref="y", text=f"{mean_wind_speed_kmh} km/h", showarrow=False,
                       font=dict(size=36, color="black"), bgcolor="rgba(255, 255, 255, 0.8)", borderpad=4)

    # Hide axes and keep the compass round at any width
    fig.update_xaxes(range=[0, 1], visible=False)
    fig.update_yaxes(range=[0, 1], visible=False, scaleanchor="x", scaleratio=1)
    fig.update_layout(
        height=380, margin=dict(t=50, b=30, l=30, r=30), plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(255,255,255,0.92)",
        # The degrees say where the wind comes from; the arrow shows where it blows to
        title=dict(text=f"Wind: {mean_wind_speed_kmh} km/h from {COMPASS_16[int((mean_direction + 11.25) % 360 // 22.5)]} "
                        f"({round(mean_direction) % 360}°)", font=title_font)
    )
    return fig


# ----------------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------------
header = html.Div(
    [
        html.Div(
            ["Experimental Environmental", html.Br(), "Monitoring Station"],
            className="title-foreground",
            style={"textAlign": "left", "lineHeight": "1.2", "fontSize": "24px", "fontWeight": "bold",
                   "color": "#333", "flex": "1"}
        ),
        html.Img(src=app.get_asset_url('icons/SKG_weather_station_logo-removebg-preview.png'),
                 style={"height": "50px", "width": "auto"})
    ],
    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center", "padding": "20px",
           "border": "1px solid #ccc", "borderRadius": "10px", "margin": "0 auto", "maxWidth": "500px",
           "backgroundColor": "rgba(255, 255, 255, 0.8)", "marginBottom": "30px"}
)

footer = html.Footer(
    children=[
        "Weather data & visualizations © ",
        f"{date.today().year} ",
        html.A("Dimitris Mitropoulos", href="https://www.linkedin.com/in/wx42", target="_blank", style={"color": "#0077B5"}),
        html.Br(),
        "All rights reserved."
    ],
    style={"position": "fixed", "bottom": "10px", "right": "10px", "fontSize": "12px", "color": "#666",
           "fontFamily": "Roboto, sans-serif", "textAlign": "right"}
)

tab_style = {
    'padding': '8px 12px', 'fontSize': '14px', 'fontFamily': 'Roboto, sans-serif', 'border': '1px solid #ccc',
    'borderBottom': 'none', 'borderRadius': '5px 5px 0 0', 'backgroundColor': '#f8f8f8', 'margin': '2px 2px 0 2px',
    'cursor': 'pointer', 'minHeight': '40px', 'display': 'flex', 'alignItems': 'center', 'justifyContent': 'center',
    'textAlign': 'center', 'flex': '1 1 0', 'minWidth': '120px'
}
tab_selected_style = {**tab_style, 'borderBottom': '1px solid white', 'backgroundColor': 'white', 'fontWeight': 'bold'}

TABS = [
    ("Dashboard", "Dashboard"),
    ("Weather Summary", "Weather Summary"),
    ("Temperature", "Temperature"),
    ("Relative Humidity", "Humidity"),
    ("Atmospheric Pressure", "Atmospheric Pressure"),
    ("Ambient Visible Light", "Light Intensity"),
    ("UV Index", "UV Index"),
    ("Wind Rose", "Wind Rose"),
    ("Wind Time Series", "Wind Time Series"),
    ("Air Quality Monitoring", "Air Quality Monitoring"),
    ("Rain Accumulation", "Rain Accumulation"),
    ("History", "History"),
    ("Cloud Camera", "Cloud Camera"),
    ("Satellite Greece", "Satellite Greece"),
    ("Temperature Anomaly", "Temperature Anomaly"),
    ("Upper Air Sounding", "Upper Air Sounding"),
    *([("Nowcast", "Nowcast")] if SHOW_NOWCAST_TAB else []),
    ("7 Day Forecast", "7 Day Forecast"),
    ("The Station", "The Station"),
]

station_status = html.Div(id="station-status", style={"textAlign": "center", "marginBottom": "20px"})

app.layout = html.Div(
    [
        header,
        station_status,  # Displays last read timestamp and online/offline status
        dcc.Interval(id="interval", interval=REFRESH_MS),
        dcc.Store(id="content-version"),
        dcc.Tabs(
            id="tabs",
            value="Dashboard",
            children=[dcc.Tab(label=label, value=value, style=tab_style, selected_style=tab_selected_style)
                      for label, value in TABS],
            persistence=True,
            persistence_type="session",
            style={'display': 'flex', 'flexWrap': 'wrap'},
            content_style={'border': '1px solid #ccc', 'borderTop': 'none', 'padding': '10px'}
        ),
        dcc.Loading(html.Div(id="tabs-content"), type="circle", delay_show=400),
        footer
    ],
    style={
        "padding": "40px 12px",
        "maxWidth": "1200px",
        "margin": "0 auto",
        "backgroundColor": "#f8f8f8",
        "backgroundImage": "url('/assets/background.png')",
        "backgroundRepeat": "repeat",
        "backgroundSize": "contain",
        "color": "#333333",
        "fontFamily": "Roboto, sans-serif"
    }
)

# ----------------------------------------------------------------------------
# History tab: any parameters from the SQLite DB over any time range
# ----------------------------------------------------------------------------
DB_PATH = "/home/dimitris/weather_station.db"
HISTORY_MAX_POINTS = 1500   # per line: the Pi averages the readings into at most this many bins
HISTORY_MAX_PARAMS = 8
HISTORY_BINS = [60, 120, 300, 600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400]
HISTORY_PRESETS = {"1d": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30), "365d": timedelta(days=365)}
# key: (label, SQL aggregate per bin or None when computed specially, unit, colour, how to draw)
HISTORY_PARAMS = {
    "t_mcp": ("Temperature (MCP9808)", f'AVG("{COL_T_MCP}")', "°C", "#d62728", "lines"),
    "t_bme": ("Temperature (BME280)", f'AVG("{COL_T_BME}")', "°C", "#1f77b4", "lines"),
    "rh": ("Relative humidity (BME280)", f'AVG("{COL_RH}")', "%", "#17a2b8", "lines"),
    "p": ("Pressure (BME280)", f'AVG("{COL_P}")', "hPa", "#6f42c1", "lines"),
    "ws": ("Wind speed (mean)", f'AVG("{COL_WS}")', "m/s", "#2ca02c", "lines"),
    "ws_max": ("Wind speed (highest reading)", f'MAX("{COL_WS}")', "m/s", "#7fbf7b", "lines"),
    "wd": ("Wind direction (vector mean)", None, "°", "#8c564b", "markers"),
    "rain": ("Rain", None, "mm", "#1565c0", "rain"),
    "rain_wet": ("Rain sensor wet (LM393, share of readings)",
                 f"""100.0 * SUM(CASE WHEN "{COL_RAIN_EVENT}" = 'Rain' THEN 1 ELSE 0 END) / NULLIF(COUNT("{COL_RAIN_EVENT}"), 0)""",
                 "%", "#4fa3e0", "lines"),
    "lux": ("Light (VEML7700)", f'AVG("{COL_LUX}")', "lux", "#ff7f0e", "lines"),
    "uv": ("UV index", f'AVG("{COL_UV}")', "", "#9467bd", "lines"),
    "pm1": ("PM1.0 (PMSA003I)", f'AVG("{COL_PM1}")', "µg/m³", "#7f7f7f", "lines"),
    "pm25": ("PM2.5 (PMSA003I)", f'AVG("{COL_PM25}")', "µg/m³", "#bcbd22", "lines"),
    "pm10": ("PM10 (PMSA003I)", f'AVG("{COL_PM10}")', "µg/m³", "#8c6d31", "lines"),
    "l_dist": ("Lightning distance (nearest)", f'MIN("{COL_LIGHTNING_DIST}")', "km", "#e377c2", "markers"),
    "l_count": ("Lightning detections", f'COUNT("{COL_LIGHTNING}")', "", "#c51b7d", "bars"),
}
# The normal drawn in a parameter's panel (build_climatology.py's variables)
HISTORY_CLIMATOLOGY = {"t_mcp": "t", "t_bme": "t", "rh": "rh", "p": "p", "ws": "ws", "lux": "lux", "uv": "uv"}
_history_lock = threading.Lock()   # one DB query at a time: the page is public and the Pi is small


def history_bin(span_s):
    return next((b for b in HISTORY_BINS if span_s / b <= HISTORY_MAX_POINTS), HISTORY_BINS[-1])


def bin_label(b):
    if b < 3600: return f"{b // 60}-minute"
    if b < 86400: return f"{b // 3600}-hour"
    return "daily" if b == 86400 else (f"{b // 86400}-day" if b < 7 * 86400 else "weekly")


@lru_cache(maxsize=1)
def history_first_day(day):
    """First date in the DB (cached per day)."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        first = con.execute("SELECT MIN(timestamp) FROM station_readings").fetchone()[0]
        con.close()
        return datetime.fromisoformat(first[:19]).date()
    except Exception:
        return date(2025, 3, 3)


@lru_cache(maxsize=16)
def history_query(keys, start, end, b):
    """{key: Series indexed by bin start (UTC)} for the parameters `keys` between `start` and `end` (UTC strings)."""
    select, names = [], []
    for k in keys:
        agg = HISTORY_PARAMS[k][1]
        if agg:
            select.append(agg); names.append(k)
        elif k == "wd":
            select += [f'AVG(sin_deg("{COL_WD}"))', f'AVG(cos_deg("{COL_WD}"))']; names += ["wd_sin", "wd_cos"]
        elif k == "rain" and b < 86400:
            select.append(f'MAX("{COL_RAIN}")'); names.append("rain")   # the running total since UTC midnight
    with _history_lock:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        con.create_function("sin_deg", 1, lambda d: None if d is None else math.sin(math.radians(d)), deterministic=True)
        con.create_function("cos_deg", 1, lambda d: None if d is None else math.cos(math.radians(d)), deterministic=True)
        rows = con.execute(f"SELECT CAST(strftime('%s', timestamp) AS INTEGER) / {b} AS bin, {', '.join(select)} "
                           f"FROM station_readings WHERE timestamp >= ? AND timestamp < ? GROUP BY bin ORDER BY bin",
                           (start, end)).fetchall() if select else []
        daily = []
        if "rain" in keys and b >= 86400:   # daily totals (the day's highest running total), summed per bin
            daily = con.execute(f"""SELECT CAST(strftime('%s', timestamp) AS INTEGER) / 86400 AS day, MAX("{COL_RAIN}")
                                    FROM station_readings WHERE timestamp >= ? AND timestamp < ? AND "{COL_RAIN}" IS NOT NULL
                                    GROUP BY day""", (start, end)).fetchall()
        con.close()
    out = {}
    if rows:
        df = pd.DataFrame(rows, columns=["bin"] + names)
        df.index = pd.to_datetime(df.pop("bin") * b, unit="s", utc=True)
        for k in keys:
            if k == "wd":
                s = (np.degrees(np.arctan2(df["wd_sin"], df["wd_cos"])) % 360).dropna()
            elif k in df.columns:
                s = df[k].dropna()
                if k == "l_count": s = s[s > 0]
            else:
                continue
            out[k] = s
    if daily:
        d = pd.Series([v for _, v in daily], index=pd.to_datetime([day * 86400 for day, _ in daily], unit="s", utc=True))
        out["rain"] = d.resample(f"{b}s", origin="epoch").sum(min_count=1).dropna()
    return out


def history_range(preset, start_date, end_date):
    """(start, end) as UTC datetimes, or None when custom dates are incomplete."""
    now = datetime.now(ZoneInfo("UTC")).replace(tzinfo=None)
    if preset in HISTORY_PRESETS:
        return now - HISTORY_PRESETS[preset], now
    if preset == "all":
        return datetime.combine(history_first_day(date.today()), datetime.min.time()), now
    if start_date and end_date:
        start = datetime.fromisoformat(start_date[:10])
        end = datetime.fromisoformat(end_date[:10]) + timedelta(days=1)
        return (start, end) if end > start else None
    return None


def history_figure(keys, start, end, show_normal=True):
    b = history_bin((end - start).total_seconds())
    # Round the ends to the bin, so repeated requests reuse the cached query
    q_start = (start - datetime(1970, 1, 1)).total_seconds() // b * b
    q_end = -(-(end - datetime(1970, 1, 1)).total_seconds() // b) * b
    to_str = lambda s: datetime.utcfromtimestamp(s).strftime("%Y-%m-%d %H:%M:%S")
    data = history_query(tuple(keys), to_str(q_start), to_str(q_end), b)
    n = len(keys)
    fig = make_subplots(rows=n, cols=1, shared_xaxes=True, vertical_spacing=min(0.06, 0.25 / n),
                        subplot_titles=[HISTORY_PARAMS[k][0] + (f" ({HISTORY_PARAMS[k][2]})" if HISTORY_PARAMS[k][2] else "")
                                        for k in keys])
    max_gap = timedelta(seconds=max(3 * b, 1800))
    normal_shown = False
    for row, k in enumerate(keys, start=1):
        label, _, unit, color, how = HISTORY_PARAMS[k]
        if show_normal and k in HISTORY_CLIMATOLOGY:
            c = climatology_binned(HISTORY_CLIMATOLOGY[k], q_start, q_end, b)
            if c is not None and not c.empty:
                add_climatology(fig, HISTORY_CLIMATOLOGY[k], unit, 0 if k == "lux" else 1, c=c, row=row, col=1)
                normal_shown = True
        s = data.get(k)
        if s is None or s.empty:
            fig.add_annotation(text="No data in this period", xref=f"x{row if row > 1 else ''} domain",
                               yref=f"y{row if row > 1 else ''} domain", x=0.5, y=0.5, showarrow=False,
                               font=dict(size=13, color="#888"))
            continue
        hover = f"%{{y:.2f}} {unit}<extra>{label}</extra>"
        if how == "bars" or (how == "rain" and b >= 86400):
            fig.add_trace(go.Bar(x=s.index, y=s.values, marker_color=color, name=label, hovertemplate=hover), row=row, col=1)
        elif how == "markers":
            fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="markers", marker=dict(color=color, size=4),
                                     name=label, hovertemplate=hover), row=row, col=1)
        else:
            s = with_gaps(s, max_gap)
            fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", line=dict(color=color, width=1.6,
                                     shape="hv" if how == "rain" else "linear"), name=label, hovertemplate=hover),
                          row=row, col=1)
        if k == "wd":
            fig.update_yaxes(range=[0, 360], tickvals=[0, 90, 180, 270, 360], row=row, col=1)
    fig.update_layout(template="plotly_white", height=140 + 210 * n, showlegend=False, hovermode="x unified",
                      font=dict(family=FONT_FAMILY, size=12, color="#333"), margin=dict(l=60, r=20, t=40, b=45),
                      paper_bgcolor="rgba(255,255,255,0.92)", dragmode="zoom")
    fig.update_xaxes(type="date", range=[start, end], showline=True, linecolor="#999", gridcolor="#eee",
                     hoverformat="%d %b %Y %H:%M UTC")
    fig.update_xaxes(title=dict(text="Time (UTC)"), row=n, col=1)
    fig.update_yaxes(showline=True, linecolor="#999", gridcolor="#eee", zeroline=False)
    fig.update_annotations(font_size=14)
    return fig, b, normal_shown


def history_tab():
    first = history_first_day(date.today())
    label_style = {"fontWeight": "bold", "fontSize": "14px", "marginBottom": "4px", "display": "block"}
    return html.Div([
        html.Div([
            html.Div([html.Label("Parameters (up to 8)", style=label_style),
                      dcc.Dropdown(id="hist-params", value=["t_mcp"], multi=True, clearable=False,
                                   options=[{"label": v[0], "value": k} for k, v in HISTORY_PARAMS.items()],
                                   persistence=True, persistence_type="session")],
                     style={"flex": "2 1 320px", "minWidth": "260px"}),
            html.Div([html.Label("Time range", style=label_style),
                      dcc.RadioItems(id="hist-preset", value="7d", inline=True, persistence=True, persistence_type="session",
                                     options=[{"label": l, "value": v} for l, v in (("24 h", "1d"), ("7 days", "7d"),
                                              ("30 days", "30d"), ("1 year", "365d"), ("All", "all"), ("Custom", "custom"))],
                                     inputStyle={"marginRight": "4px"}, labelStyle={"marginRight": "12px"}),
                      dcc.DatePickerRange(id="hist-dates", min_date_allowed=first, max_date_allowed=date.today(),
                                          initial_visible_month=date.today(), display_format="D MMM YYYY",
                                          start_date_placeholder_text="From", end_date_placeholder_text="To",
                                          persistence=True, persistence_type="session", style={"marginTop": "6px"}),
                      dcc.Checklist(id="hist-normal", value=["on"], persistence=True, persistence_type="session",
                                    options=[{"label": f"Show the {CLIM_PERIOD} normal (grey)", "value": "on"}],
                                    inputStyle={"marginRight": "4px"}, style={"marginTop": "8px"})],
                     style={"flex": "1 1 300px", "minWidth": "260px"}),
        ], style={"display": "flex", "flexWrap": "wrap", "gap": "16px", "padding": "8px"}),
        html.Div(id="hist-note", style={"fontSize": "13px", "color": "#555", "padding": "4px 8px"}),
        dcc.Loading(dcc.Graph(id="hist-graph", config={"displaylogo": False, "responsive": True,
                                                       "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}),
                    type="circle"),
    ], style={"backgroundColor": "rgba(255,255,255,0.92)", "padding": "6px"})


@app.callback(Output("hist-preset", "value"), Input("hist-dates", "start_date"), Input("hist-dates", "end_date"),
              prevent_initial_call=True)
def history_pick_custom(start_date, end_date):
    """Picking dates switches the range to Custom."""
    if start_date and end_date:
        return "custom"
    raise PreventUpdate


@app.callback(Output("hist-graph", "figure"), Output("hist-note", "children"),
              Input("hist-params", "value"), Input("hist-preset", "value"),
              Input("hist-dates", "start_date"), Input("hist-dates", "end_date"), Input("hist-normal", "value"))
def update_history(keys, preset, start_date, end_date, normal):
    keys = [k for k in (keys or []) if k in HISTORY_PARAMS]
    if not keys:
        return message_figure("Choose at least one parameter"), ""
    note = ""
    if len(keys) > HISTORY_MAX_PARAMS:
        keys, note = keys[:HISTORY_MAX_PARAMS], f"Showing the first {HISTORY_MAX_PARAMS} parameters. "
    rng = history_range(preset, start_date, end_date)
    if rng is None:
        return message_figure("Pick a start and an end date"), note
    try:
        fig, b, normal_shown = history_figure(keys, *rng, show_normal=bool(normal))
    except Exception as e:
        print(f"History tab error: {type(e).__name__}: {e}")
        return message_figure("The database is busy or unavailable; try again in a minute."), note
    note += (f"{rng[0]:%d %b %Y %H:%M} to {rng[1]:%d %b %Y %H:%M} UTC. Each point is a {bin_label(b)} average "
             "(wind direction: vector mean; highest wind reading: maximum; lightning: nearest distance or number "
             "of detections; rain: running total since UTC midnight, or daily totals for long ranges).")
    if normal_shown:
        note += climatology_note(b)
    return fig, note


# ----------------------------------------------------------------------------
# Callbacks
# ----------------------------------------------------------------------------
# Columns whose newest reading decides whether a data tab needs re-rendering
TAB_COLUMNS = {
    "Dashboard": [COL_T_MCP, COL_WS],
    "Temperature": [COL_T_MCP, COL_T_BME],
    "Humidity": [COL_RH],
    "Atmospheric Pressure": [COL_P],
    "Light Intensity": [COL_LUX],
    "UV Index": [COL_UV],
    "Wind Rose": [COL_WS],
    "Wind Time Series": [COL_WS],
    "Air Quality Monitoring": [COL_PM25],
    "Rain Accumulation": [COL_RAIN],
}
IMAGE_TABS = {
    "Cloud Camera": ("cloud-camera", "Latest whole-sky camera image", "No image found in whole_sky_camera folder."),
    "Satellite Greece": ("satellite", "Latest satellite image of Greece", "No satellite image found."),
    "Temperature Anomaly": ("anomaly", "Latest 850 hPa temperature anomaly map", "No temperature anomaly map found."),
    "7 Day Forecast": ("forecast", "7-day forecast for Kalamaria", "7-day forecast image not found."),
}
SOUNDING_IMAGES = ["sounding", "sounding-model", "sounding-sofia", "sounding-athens", "sounding-larissa"]
STATION_PHOTO = os.path.join(BASE_DIR, "assets", "station_photo.jpg")

# Satellite animation: process_image.py keeps 1600-px frames named <YYYYmmddHHMM>.jpg (image time, UTC)
SATELLITE_HISTORY_DIR = os.path.join(BASE_DIR, "satellite_latest", "history")
SATELLITE_FRAMES = 5      # how many of the newest frames the animation plays
SATELLITE_FRAME_MS = 800  # time per frame
SATELLITE_HOLD = 3        # extra frame-times spent on the newest image before the loop restarts


def satellite_frames():
    """The newest SATELLITE_FRAMES frame file names, oldest first."""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(SATELLITE_HISTORY_DIR, "[0-9]*.jpg")))[-SATELLITE_FRAMES:]


@server.route("/satellite-frame/<name>")
def satellite_frame(name):
    # Frame files never change once written, so browsers may cache them for good
    if not (len(name) == 16 and name[:12].isdigit() and name.endswith(".jpg")):
        flask.abort(404)
    path = os.path.join(SATELLITE_HISTORY_DIR, name)
    if not os.path.exists(path):
        flask.abort(404)
    with open(path, "rb") as f:
        resp = flask.Response(f.read(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def animation(prefix, frames, alt, caption, frame_ms):
    """frames (dicts with src and label, oldest first) as a loop that plays in the browser.
    register_animation(prefix, ...) must be called once for the same prefix."""
    return html.Div(
        [
            html.Img(id=f"{prefix}-frame", src=frames[-1]["src"], alt=alt,
                     style={"maxWidth": "100%", "height": "auto"}),
            html.Div(frames[-1]["label"], id=f"{prefix}-label",
                     style={"fontSize": "14px", "color": "#444", "marginTop": "6px", "fontWeight": "bold"}),
            html.Div(caption, style={"fontSize": "13px", "color": "#666", "marginTop": "2px"}),
            # Hidden copies make the browser download every frame up front, so the loop doesn't flicker
            html.Div([html.Img(src=f["src"], alt="") for f in frames], style={"display": "none"}),
            dcc.Store(id=f"{prefix}-frames", data=frames),
            dcc.Interval(id=f"{prefix}-timer", interval=frame_ms),
        ],
        style={"textAlign": "center", "padding": "10px"}
    )


def register_animation(prefix, hold):
    """Steps through the frames in the browser, so there is no request to the Pi per frame.
    The newest frame stays for 1 + hold ticks, then the loop starts again from the oldest."""
    app.clientside_callback(
        """
        function(n, frames) {
            if (!frames || frames.length === 0) {
                return [window.dash_clientside.no_update, window.dash_clientside.no_update];
            }
            var i = Math.min((n || 0) % (frames.length + HOLD), frames.length - 1);
            return [frames[i].src, frames[i].label];
        }
        """.replace("HOLD", str(int(hold))),
        Output(f"{prefix}-frame", "src"),
        Output(f"{prefix}-label", "children"),
        Input(f"{prefix}-timer", "n_intervals"),
        State(f"{prefix}-frames", "data"),
    )


def satellite_tab():
    """The last SATELLITE_FRAMES satellite images as a loop."""
    names = satellite_frames()
    if not names:  # no frames yet: the single latest image, as before
        return image_tab(*IMAGE_TABS["Satellite Greece"])
    frames = []
    for i, name in enumerate(names, 1):
        when = datetime.strptime(name[:12], "%Y%m%d%H%M")
        frames.append({"src": f"/satellite-frame/{name}",
                       "label": f"Image {i} of {len(names)}: {when:%d %b %Y %H:%M} UTC"})
    first_time = datetime.strptime(names[0][:12], "%Y%m%d%H%M")
    last_time = datetime.strptime(names[-1][:12], "%Y%m%d%H%M")
    return animation("sat", frames, "Satellite images of Greece, animated",
                     f"The last {len(names)} images, {first_time:%H:%M}-{last_time:%H:%M} UTC, "
                     "in a loop that pauses on the newest. Source: infoplaza.io",
                     SATELLITE_FRAME_MS)


register_animation("sat", SATELLITE_HOLD)


# Sky camera animation: camera_capture.sh takes a photo every 5 min (04-18 UTC), named
# <dd-mm-YYYY_HH:MM:SS>.jpg in Athens time; THUNDER_* files are lightning captures and are skipped.
CAMERA_DIR = os.path.join(BASE_DIR, "whole_sky_camera")
CAMERA_FRAMES = 10        # how many of the newest photos the animation plays
CAMERA_FRAME_MS = 400     # time per photo
CAMERA_PAUSE_MS = 2000    # how long the newest photo stays before the loop restarts
CAMERA_HOLD = CAMERA_PAUSE_MS // CAMERA_FRAME_MS - 1
CAMERA_SETTLE_S = 15      # a photo is still being written for a few seconds after it appears
_camera_cache = {}        # name -> (mtime, scaled JPEG bytes), only for the current frames


def camera_photo_time(name):
    try:
        return datetime.strptime(name[:19], "%d-%m-%Y_%H:%M:%S")
    except ValueError:
        return None


def camera_frames():
    """The newest CAMERA_FRAMES regular photo names, oldest first (by file time: names don't sort by date)."""
    now = time.time()
    photos = []
    for path in glob.glob(os.path.join(CAMERA_DIR, "[0-9]*.jpg")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:  # pruned by camera_capture.sh meanwhile
            continue
        if now - mtime >= CAMERA_SETTLE_S and camera_photo_time(os.path.basename(path)):
            photos.append((mtime, os.path.basename(path)))
    return [name for _, name in sorted(photos)[-CAMERA_FRAMES:]]


@server.route("/camera-frame/<name>")
def camera_frame(name):
    # Photos never change once written (each has its own name), so browsers may cache them for good
    current = camera_frames()
    if name not in current:
        flask.abort(404)
    path = os.path.join(CAMERA_DIR, name)
    try:
        mtime = os.path.getmtime(path)
        cached = _camera_cache.get(name)
        if not cached or cached[0] != mtime:
            cached = (mtime, _render_image(path, "JPEG"))
            _camera_cache[name] = cached
    except Exception as e:
        print(f"Camera photo {name} not ready: {e}")
        flask.abort(503)
    for old in list(_camera_cache):
        if old not in current:
            _camera_cache.pop(old, None)
    resp = flask.Response(cached[1], mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def camera_tab():
    """The last CAMERA_FRAMES sky photos as a fast loop that pauses on the newest."""
    names = camera_frames()
    if len(names) < 2:  # nothing to animate: the single latest image, as before
        return image_tab(*IMAGE_TABS["Cloud Camera"])
    times = [camera_photo_time(n) for n in names]
    frames = [{"src": f"/camera-frame/{urllib.parse.quote(name)}",
               "label": f"Photo {i} of {len(names)}: {when:%d %b %Y %H:%M} (Athens time)"}
              for i, (name, when) in enumerate(zip(names, times), 1)]
    return animation("cam", frames, "Whole-sky camera photos, animated",
                     f"The last {len(names)} photos, {times[0]:%H:%M}-{times[-1]:%H:%M} (Athens time), "
                     "one every 5 minutes, in a loop that pauses on the newest.",
                     CAMERA_FRAME_MS)


register_animation("cam", CAMERA_HOLD)


def station_tab():
    """A photo of the station (720x1280, no metadata), served from assets/ with its file time in the URL."""
    if not os.path.exists(STATION_PHOTO):
        return html.Div("Station photo not found.", style={"textAlign": "center", "padding": "20px"})
    version = int(os.path.getmtime(STATION_PHOTO))
    return html.Div(
        [
            html.Img(src=f"{app.get_asset_url('station_photo.jpg')}?v={version}",
                     alt="The weather station's mast with its instruments",
                     style={"maxWidth": "100%", "maxHeight": "80vh", "height": "auto", "borderRadius": "8px",
                            "boxShadow": "0 2px 8px rgba(0, 0, 0, 0.25)"}),
            html.Div("The station's mast in Kalamaria, Thessaloniki: cup anemometer, wind vane, "
                     "radiation shield and solar panel.",
                     style={"fontSize": "14px", "color": "#555", "marginTop": "10px"}),
        ],
        style={"textAlign": "center", "padding": "10px"}
    )


def content_version(tab, df):
    """Changes only when what the tab shows would change, so unchanged tabs aren't re-sent."""
    if tab == "History":
        # Its controls drive its own callbacks; the layout only changes with the date picker's last day
        return f"{tab}|{date.today()}"
    if tab == "The Station":
        return f"{tab}|{os.path.getmtime(STATION_PHOTO) if os.path.exists(STATION_PHOTO) else 0}"
    if tab == "Satellite Greece":
        # A new frame arriving (every ~15 min) is what changes this tab
        return f"{tab}|{satellite_frames() or latest_image('satellite')[1]}"
    if tab == "Cloud Camera":
        # A new photo (every 5 min in daytime) is what changes this tab
        return f"{tab}|{camera_frames() or latest_image('cloud-camera')[1]}"
    if tab == "Upper Air Sounding":
        # Hourly bucket: the switch to the fallbacks happens when the Thessaloniki sounding gets too old
        return f"{tab}|{[latest_image(n)[1] for n in SOUNDING_IMAGES]}|{int(time.time() // 3600)}"
    if tab in IMAGE_TABS:
        return f"{tab}|{latest_image(IMAGE_TABS[tab][0])[1]}"
    if tab == "Nowcast":
        files = glob.glob(os.path.join(BASE_DIR, "nowcasts/*"))
        return f"{tab}|{max((os.path.getmtime(f) for f in files), default=0)}"
    if df.empty:
        return f"{tab}|empty"
    cols = TAB_COLUMNS.get(tab)
    if cols is None:  # e.g. the summary table: any new row counts
        return f"{tab}|{_data_cache['key']}"
    newest = [df.loc[df[c].notna(), "timestamp"].max() for c in cols if c in df.columns]
    # The 10-minute bucket keeps the 24-hour axis moving even when a sensor is quiet
    return f"{tab}|{max((t for t in newest if pd.notna(t)), default=None)}|{int(time.time() // 600)}"


def render_tab(tab, df):
    if tab == "Dashboard":
        thermo_fig = create_thermometer_dashboard(df)

        now_utc = datetime.now(ZoneInfo("UTC"))
        df_last10 = df[df["timestamp"] >= now_utc - timedelta(minutes=10)] if not df.empty else df
        wind_fig = message_figure("No wind data")
        if not df_last10.empty and COL_WS in df_last10.columns and COL_WD in df_last10.columns:
            mean_wind_speed_mps = df_last10[COL_WS].mean()
            if not np.isnan(mean_wind_speed_mps):
                # Vector mean of the last 10 minutes: an arithmetic mean of angles breaks around north
                # (350° and 10° average to 180°), which turned the arrow round in 17% of cases
                rad = np.deg2rad(df_last10[COL_WD].dropna())
                mean_direction = (float(np.rad2deg(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean()))) % 360
                                  if len(rad) else 0.0)
                wind_fig = create_wind_direction_figure(round(mean_wind_speed_mps * 3.6, 1), mean_direction)

        box = {"flex": "1 1 340px", "padding": "10px", "minWidth": "300px"}
        return html.Div(
            [html.Div(graph(thermo_fig, 380), style=box), html.Div(graph(wind_fig, 380), style=box)],
            style={"display": "flex", "flexWrap": "wrap", "justifyContent": "space-around"}
        )
    if tab == "Weather Summary":
        return create_dashboard(df)
    if tab == "Temperature":
        return graph(create_temperature_figure(df))
    if tab == "Humidity":
        return graph(create_humidity_figure(df))
    if tab == "Atmospheric Pressure":
        return graph(create_pressure_figure(df))
    if tab == "Light Intensity":
        return graph(create_light_figure(df))
    if tab == "UV Index":
        return graph(create_uv_figure(df))
    if tab == "Wind Rose":
        box = {"flex": "1 1 330px", "minWidth": "300px"}
        roses = html.Div(
            [html.Div(graph(create_wind_rose_range(df, timedelta(hours=24), "Last 24 hours"), 400), style=box),
             html.Div(graph(create_wind_rose_range(df, timedelta(hours=1), "Last 1 hour"), 400), style=box),
             html.Div(graph(create_wind_rose_range(df, timedelta(minutes=10), "Last 10 minutes"), 400), style=box)],
            style={"display": "flex", "justifyContent": "space-around", "flexWrap": "wrap"}
        )
        return html.Div([roses, wind_speed_key()])  # one colour key for all three roses
    if tab == "Wind Time Series":
        return graph(create_wind_timeseries_figure(df))
    if tab == "Air Quality Monitoring":
        return graph(create_air_quality_figure(df))
    if tab == "Rain Accumulation":
        return graph(create_rain_figure(df))
    if tab == "Upper Air Sounding":
        return sounding_tab()
    if tab == "History":
        return history_tab()
    if tab == "The Station":
        return station_tab()
    if tab == "Satellite Greece":
        return satellite_tab()
    if tab == "Cloud Camera":
        return camera_tab()
    if tab in IMAGE_TABS:
        return image_tab(*IMAGE_TABS[tab])
    if tab == "Nowcast":
        return html.Div(
            dcc.Markdown(get_latest_nowcast_text(), style={"textAlign": "left", "padding": "20px", "lineHeight": "1.6"}),
            style={"backgroundColor": "white", "border": "1px solid #ccc", "borderRadius": "5px",
                   "margin": "20px auto", "maxWidth": "900px", "padding": "15px"}
        )
    return html.Div("No content available.", style={"textAlign": "center"})


_render_cache = {}  # tab -> (version, content): each version is rendered once, whatever the number of viewers


@app.callback(
    Output("tabs-content", "children"),
    Output("content-version", "data"),
    Input("tabs", "value"),
    Input("interval", "n_intervals"),
    State("content-version", "data"),
)
def render_content(tab, n_intervals, shown_version):
    df = load_data()
    version = content_version(tab, df)
    if ctx.triggered_id == "interval" and version == shown_version:
        raise PreventUpdate  # nothing new for this tab: don't re-send it
    cached = _render_cache.get(tab)
    if cached and cached[0] == version:
        return cached[1], version
    try:
        content = render_tab(tab, df)
    except Exception as e:
        print(f"Error rendering tab {tab}: {e}")
        return html.Div("This view is temporarily unavailable.", style={"textAlign": "center", "padding": "20px"}), None
    _render_cache[tab] = (version, content)
    return content, version


@app.callback(Output("station-status", "children"), Input("interval", "n_intervals"))
def update_station_status(n_intervals):
    df = load_data()
    if df.empty or df["timestamp"].max() is None:
        return html.Div("No data available", style={"color": "gray"})
    last_ts = df["timestamp"].max()
    last_ts_greece = last_ts.astimezone(ZoneInfo("Europe/Athens"))
    now_greece = datetime.now(ZoneInfo("Europe/Athens"))
    diff_minutes = (now_greece - last_ts_greece).total_seconds() / 60.0
    status_text = "Station is OFFLINE" if diff_minutes > 30 else "Station is Online"
    indicator_style = {"display": "inline-block", "width": "15px", "height": "15px", "borderRadius": "50%", "marginRight": "10px"}
    if diff_minutes > 30:
        indicator_style.update({"backgroundColor": "red", "animation": "blinker 1s linear infinite"})
    else:
        indicator_style.update({"backgroundColor": "green"})
    indicator_dot = html.Span("", style=indicator_style)
    ts_str = last_ts_greece.strftime("%Y-%m-%d %H:%M:%S")
    line_style = {"marginTop": "10px", "fontSize": "14px"}

    lightning_info_str = "No lightning data available"
    if COL_LIGHTNING in df.columns:
        df_lightning = df[df[COL_LIGHTNING].notnull()]
        if not df_lightning.empty:
            latest_lightning_row = df_lightning.loc[df_lightning["timestamp"].idxmax()]
            lightning_time = latest_lightning_row["timestamp"].astimezone(ZoneInfo("Europe/Athens")).strftime("%Y-%m-%d %H:%M")
            lightning_distance = latest_lightning_row[COL_LIGHTNING_DIST]
            lightning_info_str = f"Last Lightning: {lightning_time} (Distance: {lightning_distance} km)"
        else:
            lightning_info_str = "No lightning detected"
    lightning_info = html.Div(lightning_info_str, style=line_style)

    rain_info = html.Div("No rain data available", style=line_style)
    if COL_RAIN_EVENT in df.columns:
        df_rain = df[df[COL_RAIN_EVENT].notnull()]
        if not df_rain.empty:
            latest_rain_value = str(df_rain.iloc[-1][COL_RAIN_EVENT]).strip()
            if latest_rain_value.lower() == "rain":
                rain_indicator_dot = html.Span("", style={"display": "inline-block", "width": "15px", "height": "15px", "borderRadius": "50%", "backgroundColor": "blue", "marginLeft": "10px", "animation": "blinker 1s linear infinite"})
                rain_info = html.Div(["Rain is happening now", rain_indicator_dot], style=line_style)
            elif latest_rain_value.lower() == "no rain":  # the board sends "no Rain"
                rain_info = html.Div("No rain at the moment", style=line_style)
            else:
                rain_info = html.Div(f"Rain status: {latest_rain_value}", style=line_style)
        else:
            rain_info = html.Div("No recent rain data", style=line_style)

    try:
        moon_info = html.Div(get_moon_phase_and_rise_info(), style={**line_style, "fontFamily": "Roboto, sans-serif"})
    except Exception as e:
        print(f"Error getting moon phase and rise info: {e}")
        moon_info = html.Div("Moon Phase & Rise: N/A", style={**line_style, "fontFamily": "Roboto, sans-serif"})

    return html.Div([
        indicator_dot, html.Span(f"{status_text} (Last data at: {ts_str})"),
        lightning_info, rain_info, moon_info],
        style={"fontSize": "16px", "fontFamily": "Roboto, sans-serif", "padding": "20px", "border": "1px solid #ccc", "borderRadius": "10px", "margin": "0 auto", "maxWidth": "500px", "backgroundColor": "rgba(255, 255, 255, 0.8)", "marginBottom": "30px", "textAlign": "center"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=False)
