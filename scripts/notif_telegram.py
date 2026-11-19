import os, glob, base64, json, io
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

import asyncio
import telegram
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    print("Error: TELEGRAM_BOT_TOKEN not found. Make sure it's set in your .env file.")



bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

CSV_PATH = "/home/dimitris/weather_station/weather_station_data.csv"

# The whole CSV is ~33 MB and parsing it took ~6 s per call, so only its end is read: a day is ~130 KB.
TAIL_START_BYTES = 512_000  # ~4 days; doubled until the window reaches back far enough


def load_data_since(since_utc):
    """
    Rows dated from `since_utc` on, with timestamps as UTC and the sensor columns numeric, parsed from the
    end of the CSV only. The window doubles until its oldest row is an hour older than `since_utc`
    (rows from different writers aren't strictly in time order), or covers the whole file.
    """
    try:
        since_naive = since_utc.astimezone(timezone.utc).replace(tzinfo=None)
        size = os.path.getsize(CSV_PATH)
        with open(CSV_PATH, "rb") as f:
            header = f.readline()
            window = TAIL_START_BYTES
            while True:
                start = max(len(header), size - window)
                f.seek(start)
                chunk = f.read()
                if start > len(header):
                    chunk = chunk[chunk.find(b"\n") + 1:]  # the first line is cut
                df = pd.read_csv(io.BytesIO(header + chunk), dtype={"timestamp": str},
                                 low_memory=False, on_bad_lines="skip")
                df["timestamp"] = pd.to_datetime(df["timestamp"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
                df = df.dropna(subset=["timestamp"])
                if start <= len(header) or (not df.empty and df["timestamp"].min() <= since_naive - timedelta(hours=1)):
                    break
                window *= 2
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
        df = df[df["timestamp"] >= since_utc]
        numeric_cols = [col for col in df.columns if col not in ["timestamp", "Lightning Detection (AS3935)", "Rain Event (LM393)"]]
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
        return df
    except Exception as e:
        print("Error loading data:", e)
        return pd.DataFrame()


def load_data():
    """For event-based checks: the last 24 hours of data."""
    return load_data_since(datetime.now(timezone.utc) - timedelta(hours=24))

# ------------------------------
# Notification Functions
# ------------------------------

def check_min_temp_notification(today_date):
    """
    If current UTC time is between 07:00 and 07:05 and no minimum notification has been sent today,
    return the minimum temperature notification message (using the MCP9808 sensor).
    """
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    if now_utc.hour == 7 and now_utc.minute < 5:
        midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        df = load_data_since(midnight_utc)
        # SAFETY CHECK: Ensure data and timestamp column exist
        if df.empty or "timestamp" not in df.columns:
            return None
        df_today = df[df["timestamp"] >= midnight_utc]
        if df_today.empty or "Temperature (MCP9808) (°C)" not in df_today.columns:
            return None
        min_temp = df_today["Temperature (MCP9808) (°C)"].min()
        if np.isnan(min_temp):
            return None
        return f"Daily Minimum (MCP9808): {min_temp:.1f}°C recorded since midnight UTC."
    return None

def check_max_temp_notification(today_date):
    """
    If current UTC time is between 15:00 and 15:05 and no maximum notification has been sent today,
    return the maximum temperature notification message (using the MCP9808 sensor).
    """
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    if now_utc.hour == 15 and now_utc.minute < 5:
        midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        df = load_data_since(midnight_utc)
        # SAFETY CHECK: Ensure data and timestamp column exist
        if df.empty or "timestamp" not in df.columns:
            return None
        df_today = df[df["timestamp"] >= midnight_utc]
        if df_today.empty or "Temperature (MCP9808) (°C)" not in df_today.columns:
            return None
        max_temp = df_today["Temperature (MCP9808) (°C)"].max()
        if np.isnan(max_temp):
            return None
        return f"Daily Maximum (MCP9808): {max_temp:.1f}°C recorded since midnight UTC."
    return None

def check_rain_notification():
    df = load_data()
    col = "Rain Event (LM393)"
    # SAFETY CHECK: Exit if data failed to load or column is missing
    if df.empty or "timestamp" not in df.columns or col not in df.columns:
        return None
    df = df.sort_values("timestamp")

    # Keep only non-null, non-blank event rows
    events = (
        df[col]
        .dropna()                   # remove NaNs
        .astype(str)                # ensure string type
        .str.strip()                # trim whitespace
        .reset_index(drop=True)
    )
    if len(events) < 2:
        return None

    prev_event, last_event = events.iloc[-2], events.iloc[-1]
    if last_event.lower() == "rain" and prev_event.lower() == "no rain":
        # get timestamp of that last event row
        ts = df[df[col].notna()].iloc[-1]["timestamp"]
        event_time = ts.astimezone(ZoneInfo("Europe/Athens")).strftime("%H:%M")
        return f"Rain detected at {event_time} (rain started)."
    return None

_last_lightning_time = None  # module-level

def check_lightning_notification():
    global _last_lightning_time
    df = load_data()
    col = "Lightning Distance (AS3935) (km)"
    # SAFETY CHECK: Exit if data failed to load or column is missing
    if df.empty or "timestamp" not in df.columns or col not in df.columns:
        return None
    df = df.sort_values("timestamp")

    # Keep only event rows with a valid float distance
    events = df[[ "timestamp", col ]].dropna(subset=[col])
    if events.empty:
        return None

    # The very latest lightning event:
    last_event = events.iloc[-1]
    event_time = last_event["timestamp"].astimezone(ZoneInfo("Europe/Athens"))
    # Only notify if it’s new
    if _last_lightning_time != event_time:
        _last_lightning_time = event_time
        hhmm = event_time.strftime("%H:%M")
        dist = last_event[col]
        return f"Lightning detected at {hhmm} with distance {dist:.1f} km."
    return None

# ------------------------------
# Alerts when a measurement stops (and when it comes back)
# ------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = os.path.join(BASE_DIR, "weather_station_data.csv")
ALERT_STATE_FILE = os.path.join(BASE_DIR, "data_alerts_state.json")  # survives restarts: no repeated alerts

# (CSV column, name used in the messages, board that sends it)
WATCHED = [
    ("Temperature (MCP9808) (°C)", "MCP9808 temperature", "temperature board"),
    ("Temperature (BME280) (°C)", "BME280 temperature", "temperature board"),
    ("Humidity (BME280) (%)", "humidity (BME280)", "temperature board"),
    ("Pressure (BME280) (hPa)", "pressure (BME280)", "temperature board"),
    ("Light Intensity (BH1750) (lux)", "light", "UV/light board"),  # the sensor is now a VEML7700
    ("UV Index (GY-8511)", "UV index", "UV/light board"),
]
BOARD_HINTS = {
    "temperature board": "Nothing at all from the temperature board (ESP32 on USB socket 1-1.2).",
    "UV/light board": "Nothing at all from the UV/light board (ESP32 on USB socket 1-1.4).",
}
# The temperature board's rows sometimes go missing 5-10 in a row (9-18 min, the longest 26 min) many
# times a day, so a measurement counts as stopped only after 30 minutes without a reading.
STALE_AFTER = timedelta(minutes=30)
GAP_CHECK_EVERY = timedelta(minutes=1)
STARTUP_GRACE = timedelta(minutes=5)  # after a reboot, give the USB logger time to write again
GAP_TAIL_BYTES = 2_000_000            # about a week of CSV


def newest_readings(now_utc):
    """Newest reading time (UTC) of each watched column in the last GAP_TAIL_BYTES of the CSV, and the
    oldest row time looked at (a column without readings has been quiet since at least then)."""
    with open(DATA_CSV, "rb") as f:
        header = f.readline().decode("utf-8", "ignore").strip().split(",")
        f.seek(0, 2)
        f.seek(max(0, f.tell() - GAP_TAIL_BYTES))
        lines = f.read().decode("utf-8", "ignore").splitlines()[1:]  # the first line may be cut
    columns = {col: header.index(col) for col, _, _ in WATCHED}
    newest = dict.fromkeys(columns)
    oldest = None
    latest_allowed = now_utc + timedelta(minutes=5)  # ignore rows dated in the future (NTP glitches)
    for line in lines:
        fields = line.split(",")
        try:
            t = datetime.fromisoformat(fields[0]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t > latest_allowed:
            continue
        if oldest is None or t < oldest:
            oldest = t
        for col, i in columns.items():
            if i < len(fields) and fields[i].strip() and (newest[col] is None or t > newest[col]):
                newest[col] = t
    return newest, oldest


def athens_time(t, now_utc):
    local = t.astimezone(ZoneInfo("Europe/Athens"))
    same_day = local.date() == now_utc.astimezone(ZoneInfo("Europe/Athens")).date()
    return local.strftime("%H:%M" if same_day else "%d %b %H:%M")


def check_data_stopped(state, now_utc):
    """
    Messages about watched measurements that have stopped (no reading for STALE_AFTER) or come back.
    `state` maps each stopped measurement's name to its last reading time (ISO, UTC) and is updated in place.
    """
    newest, oldest = newest_readings(now_utc)
    stopped, back = [], []
    for col, name, board in WATCHED:
        last = newest[col]
        if last is None or now_utc - last > STALE_AFTER:
            if name not in state:
                stopped.append((name, board, last))
                state[name] = (last or oldest).isoformat() if (last or oldest) else None
        elif name in state:
            since_iso = state.pop(name)
            back.append((name, datetime.fromisoformat(since_iso) if since_iso else None, last))

    messages = []
    if stopped:
        lines = ["⚠️ Weather station: measurements stopped (no reading for over "
                 f"{int(STALE_AFTER.total_seconds() // 60)} min)"]
        for name, board, last in stopped:
            if last:
                lines.append(f"• {name}: last reading at {athens_time(last, now_utc)}")
            else:
                lines.append(f"• {name}: no reading since at least {athens_time(oldest, now_utc)}" if oldest
                             else f"• {name}: no reading found")
        for board in dict.fromkeys(b for _, b, _ in stopped):
            board_names = [n for _, n, b in WATCHED if b == board]
            if all(n in state for n in board_names):
                lines.append(BOARD_HINTS[board] + " Check that usb_weather_logger_dual.py is running (it can lose "
                             "one board and keep reading the other), then the board and its USB cable.")
            else:
                lines.append(f"The {board} still sends its other readings, so the sensor itself or its wiring "
                             "has stopped answering.")
        messages.append("\n".join(lines))
    if back:
        lines = ["✅ Weather station: measurements back"]
        for name, since, last in back:
            gap = f", no readings from {athens_time(since, now_utc)} to {athens_time(last, now_utc)}" if since else ""
            lines.append(f"• {name}{gap}")
        messages.append("\n".join(lines))
    return messages


def load_alert_state():
    try:
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_alert_state(state):
    tmp = ALERT_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, ALERT_STATE_FILE)


# ------------------------------
# Async function to send a Telegram notification
# ------------------------------
async def send_notification(message):
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

# ------------------------------
# Main async loop to continuously check and send notifications
# ------------------------------
async def main_loop():
    # Track the date when the daily min and max notifications were sent.
    sent_min_date = None
    sent_max_date = None

    # You might also want to track previous event messages for rain or lightning to avoid duplicates.
    previous_rain_notification = None
    previous_lightning_notification = None

    # Measurements that have stopped (persisted, so a restart doesn't repeat the alert)
    alert_state = load_alert_state()
    started = datetime.now(timezone.utc)
    last_gap_check = None

    while True:
        notifications = []
        
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        today_date = now_utc.date()

        # 1) Daily minimum temperature notification at 07:00 UTC
        min_msg = check_min_temp_notification(today_date)
        if min_msg and (sent_min_date != today_date):
            notifications.append(min_msg)
            sent_min_date = today_date  # mark today's min as sent

        # 2) Daily maximum temperature notification at 15:00 UTC
        max_msg = check_max_temp_notification(today_date)
        if max_msg and (sent_max_date != today_date):
            notifications.append(max_msg)
            sent_max_date = today_date  # mark today's max as sent

        # 3) Event-based: Rain notification when it changes from "No Rain" to "Rain"
        rain_msg = check_rain_notification()
        if rain_msg and rain_msg != previous_rain_notification:
            notifications.append(rain_msg)
            previous_rain_notification = rain_msg

        # 4) Event-based: Lightning notification with distance
        lightning_msg = check_lightning_notification()
        if lightning_msg and lightning_msg != previous_lightning_notification:
            notifications.append(lightning_msg)
            previous_lightning_notification = lightning_msg

        # Send each notification asynchronously.
        for msg in notifications:
            await send_notification(msg)
            print("Notification sent:", msg)

        # 5) Measurements that stopped or came back (temperatures, humidity, pressure, light, UV).
        #    The state is only saved once its messages are sent, so a failed send is retried next time.
        now = datetime.now(timezone.utc)
        if now - started >= STARTUP_GRACE and (last_gap_check is None or now - last_gap_check >= GAP_CHECK_EVERY):
            last_gap_check = now
            try:
                new_state = dict(alert_state)
                for msg in check_data_stopped(new_state, now):
                    await send_notification(msg)
                    print("Notification sent:", msg)
                if new_state != alert_state:
                    save_alert_state(new_state)
                alert_state = new_state
            except Exception as e:
                print(f"Data-stopped check failed: {type(e).__name__}: {e}")

        # Sleep for 19 seconds before checking again.
        await asyncio.sleep(19)

if __name__ == "__main__":
    asyncio.run(main_loop())
