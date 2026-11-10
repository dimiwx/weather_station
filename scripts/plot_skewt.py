import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
import glob
import io
import os

import requests
import metpy.calc as mpcalc
from metpy.cbook import get_test_data
from metpy.plots import SkewT
from metpy.units import units
from metpy.interpolate import interpolate_1d


UWYO_URL = "https://weather.uwyo.edu/wsgi/sounding"
# How far back to look for the newest sounding. Thessaloniki usually launches at 00Z (sometimes 06Z too),
# but on roughly 1 day in 10 there is no sounding in the archive at all.
LOOKBACK_HOURS = 72


def fetch_uwyo_sounding(dt, station_id):
    """
    Fetches one sounding as CSV from the University of Wyoming service and returns it with the
    column names siphon's WyomingUpperAir used. siphon < 0.11 still calls the retired cgi-bin
    interface (404 since late June 2026), and siphon 0.11 needs Python >= 3.11.
    """
    params = {"datetime": dt.strftime("%Y-%m-%d %H:%M:%S"), "id": station_id,
              "type": "TEXT:CSV", "src": "UNKNOWN"}
    resp = requests.get(UWYO_URL, params=params, timeout=60)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {resp.text.strip()[:120]}")

    raw = pd.read_csv(io.StringIO(resp.text), skipinitialspace=True)
    # High-resolution soundings (Athens sends ~2-s samples) repeat or briefly reverse the pressure, and
    # MetPy needs it strictly decreasing: keep a row only if its pressure is below every earlier one.
    p = raw['pressure_hPa'].values
    raw = raw[p < np.fmin.accumulate(np.r_[np.inf, p[:-1]])].reset_index(drop=True)
    speed = raw['wind speed_m/s'].values * units('m/s')
    direction = raw['wind direction_degree'].values * units.degree
    u, v = mpcalc.wind_components(speed, direction)
    return pd.DataFrame({
        'pressure': raw['pressure_hPa'],
        'height': raw['geopotential height_m'],
        'temperature': raw['temperature_C'],
        'dewpoint': raw['dew point temperature_C'],
        'u_wind': u.to('knots').magnitude,
        'v_wind': v.to('knots').magnitude,
    })


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# GFS pressure levels from the surface up to 100 hPa (the top of the plot)
MODEL_LEVELS_HPA = [1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500,
                    450, 400, 350, 300, 250, 200, 150, 100]
MODEL_VARIABLES = ["temperature", "dew_point", "wind_speed", "wind_direction", "geopotential_height"]


def fetch_model_profile(dt, lat, lon, elevation_m, model="gfs_seamless"):
    """
    Model profile on pressure levels for one hour from Open-Meteo, with the same columns as
    fetch_uwyo_sounding. Levels below the ground (when surface pressure is under 1000 hPa) are dropped.
    """
    params = {"latitude": lat, "longitude": lon, "models": model, "wind_speed_unit": "kn",
              "timezone": "UTC", "past_days": 1, "forecast_days": 1,
              "hourly": ",".join(f"{v}_{p}hPa" for v in MODEL_VARIABLES for p in MODEL_LEVELS_HPA)}
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=60)
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    i = hourly["time"].index(dt.strftime("%Y-%m-%dT%H:%M"))

    rows = []
    for p in MODEL_LEVELS_HPA:
        t, td, speed, direction, z = (hourly[f"{v}_{p}hPa"][i] for v in MODEL_VARIABLES)
        if None in (t, td, speed, direction, z) or z < elevation_m:
            continue
        rows.append((p, z, t, td, speed, direction))
    if len(rows) < 5:
        raise ValueError(f"only {len(rows)} usable model levels")
    raw = pd.DataFrame(rows, columns=["pressure", "height", "temperature", "dewpoint", "speed", "direction"])
    u, v = mpcalc.wind_components(raw["speed"].values * units.knots, raw["direction"].values * units.degree)
    return pd.DataFrame({
        'pressure': raw['pressure'],
        'height': raw['height'],
        'temperature': raw['temperature'],
        'dewpoint': raw['dewpoint'],
        'u_wind': u.to('knots').magnitude,
        'v_wind': v.to('knots').magnitude,
    })


def find_newest_sounding(station_id, save_dir):
    """
    The newest available observed sounding for a station: every 6-hourly launch time over the last
    LOOKBACK_HOURS, newest first. Returns (time, data), (time, None) when that sounding is already
    plotted, or (None, None) when there is none.
    """
    now_utc = datetime.now(timezone.utc)
    newest_slot = now_utc.replace(hour=now_utc.hour - now_utc.hour % 6, minute=0, second=0, microsecond=0)
    for dt in [newest_slot - timedelta(hours=6 * i) for i in range(LOOKBACK_HOURS // 6 + 1)]:
        # Reaching a sounding that is already plotted means nothing newer is available: stop, so
        # frequent runs cost only a few requests and the plot's file time stays its first-plotted time.
        if os.path.exists(os.path.join(save_dir, f"{station_id}_sounding_{dt:%Y%m%d_%H%M}.png")):
            print(f"Newest available sounding for {station_id} ({dt:%Y-%m-%d %H}Z) is already plotted. Nothing new.")
            return dt, None
        try:
            print(f"Attempting to fetch data for station {station_id} for {dt:%Y-%m-%d %H:%M}Z...")
            df = fetch_uwyo_sounding(dt, station_id)
            print(f"Data fetched successfully for {dt:%Y-%m-%d %H:%M}Z.")
            return dt, df
        except Exception as e:
            print(f"-> No data available for {dt:%Y-%m-%d %H}Z ({type(e).__name__}: {e}). Trying next available time.")
    print(f"No sounding available for {station_id} in the last {LOOKBACK_HOURS} hours.")
    return None, None


def plot_skewt_for_station(station_id, station_name_long, save_dir):
    """Plots the station's newest observed sounding unless it is already plotted; returns its time or None."""
    dt, df = find_newest_sounding(station_id, save_dir)
    if df is not None:
        plot_sounding(df, station_name_long, dt, os.path.join(save_dir, f"{station_id}_sounding_{dt:%Y%m%d_%H%M}.png"))
    return dt


def plot_sounding(df, station_name_long, time_val, filepath):
    """
    Calculates thermodynamic properties and instability indices from a profile (columns pressure,
    height, temperature, dewpoint, u_wind, v_wind in knots; surface first) and saves a Skew-T
    diagram with an information box.
    """
    try:
        # --- 3. Prepare Data for MetPy ---
        # Extract the necessary columns and assign units.
        p = df['pressure'].values * units.hPa
        T = df['temperature'].values * units.degC
        Td = df['dewpoint'].values * units.degC
        u = df['u_wind'].values * units.knots
        v = df['v_wind'].values * units.knots
        height = df['height'].values * units.meter
        
        # Drop rows with missing pressure, temperature, or dewpoint data
        valid_mask = ~np.isnan(p.magnitude) & ~np.isnan(T.magnitude) & ~np.isnan(Td.magnitude)
        p = p[valid_mask]
        T = T[valid_mask]
        Td = Td[valid_mask]
        u = u[valid_mask]
        v = v[valid_mask]
        height = height[valid_mask] # Apply mask to height as well

        print(f"Processing data for {station_name_long} at {time_val:%Y-%m-%d %H:%M}Z.")
        
        # --- 4. Calculate Thermodynamic Properties ---
        # Define the mixed-layer parcel using the average of the lowest 3 observations.
        if len(p) >= 3:
            sfc_pressure = p[0]
            sfc_temperature = np.mean(T[:3])
            sfc_dewpoint = np.mean(Td[:3])
            print("Using mixed-layer parcel (mean of lowest 3 points).")
        else:
            sfc_pressure = p[0]
            sfc_temperature = T[0]
            sfc_dewpoint = Td[0]
            print("Fewer than 3 points available; using surface-based parcel.")

        # Calculate the parcel profile for plotting and CAPE/CIN calculations.
        parcel_prof = mpcalc.parcel_profile(p, sfc_temperature, sfc_dewpoint).to('degC')

        # Calculate CAPE and CIN
        cape, cin = mpcalc.cape_cin(p, T, Td, parcel_prof)
        print(f"Calculated CAPE: {cape:.2f}")
        print(f"Calculated CIN: {cin:.2f}")
        
        # Calculate LCL, LFC, and EL
        lcl_pressure, lcl_temperature = mpcalc.lcl(sfc_pressure, sfc_temperature, sfc_dewpoint)
        p_for_lfc, T_for_lfc, Td_for_lfc = p, T.copy(), Td.copy()
        T_for_lfc[0], Td_for_lfc[0] = sfc_temperature, sfc_dewpoint
        lfc_pressure, lfc_temperature = mpcalc.lfc(p_for_lfc, T_for_lfc, Td_for_lfc, which='most_cape')
        el_pressure, el_temperature = mpcalc.el(p_for_lfc, T_for_lfc, Td_for_lfc, which='most_cape')

        # Interpolate height data to find the AGL height of LCL, LFC, and EL
        lcl_height = np.nan * units.meter
        lfc_height = np.nan * units.meter
        el_height = np.nan * units.meter

        if np.isfinite(lcl_pressure):
            lcl_height = interpolate_1d(lcl_pressure, p, height)[0]
        if np.isfinite(lfc_pressure):
            lfc_height = interpolate_1d(lfc_pressure, p, height)[0]
        if np.isfinite(el_pressure):
            el_height = interpolate_1d(el_pressure, p, height)[0]

        # --- 4a. Calculate Additional Instability Indices ---
        print("Calculating instability indices...")
        indices = {}
        
        # Lifted Index
        try:
            p_full = df['pressure'].values * units.hPa
            T_full = df['temperature'].values * units.degC
            valid_T_mask = ~np.isnan(p_full.magnitude) & ~np.isnan(T_full.magnitude)
            p_for_interp = p_full[valid_T_mask]
            T_for_interp = T_full[valid_T_mask]
            parcel_prof_full = mpcalc.parcel_profile(p_for_interp, sfc_temperature, sfc_dewpoint)
            p_500 = 500 * units.hPa
            t_500 = interpolate_1d(p_500, p_for_interp, T_for_interp)[0]
            parcel_t_500 = interpolate_1d(p_500, p_for_interp, parcel_prof_full)[0]
            li_val = t_500 - parcel_t_500
            indices['Lifted Index'] = f'Lifted Index: {li_val.to("delta_degC").magnitude:.2f} °C'
            print("Lifted Index calculated successfully.")
        except Exception as e:
            indices['Lifted Index'] = 'Lifted Index: N/A'
            print(f"Could not calculate Lifted Index. Error: {e}")

        # Showalter Index
        try:
            si_val = mpcalc.showalter_index(p, T, Td)
            indices['Showalter Index'] = f'Showalter Index: {si_val[0].to("delta_degC").magnitude:.2f} °C'
        except Exception:
            indices['Showalter Index'] = 'Showalter Index: N/A'

        # K Index
        try:
            p_full = df['pressure'].values * units.hPa
            T_full = df['temperature'].values * units.degC
            valid_T_mask = ~np.isnan(p_full.magnitude) & ~np.isnan(T_full.magnitude)
            p_for_T_interp = p_full[valid_T_mask]
            T_for_T_interp = T_full[valid_T_mask]
            Td_full = df['dewpoint'].values * units.degC
            valid_Td_mask = ~np.isnan(p_full.magnitude) & ~np.isnan(T_full.magnitude) & ~np.isnan(Td_full.magnitude)
            p_for_Td_interp = p_full[valid_Td_mask]
            Td_for_Td_interp = Td_full[valid_Td_mask]
            p_req_T = np.array([850, 700, 500]) * units.hPa
            T_req = interpolate_1d(p_req_T, p_for_T_interp, T_for_T_interp)
            T850, T700, T500 = T_req[0], T_req[1], T_req[2]
            p_req_Td = np.array([850, 700]) * units.hPa
            Td_req = interpolate_1d(p_req_Td, p_for_Td_interp, Td_for_Td_interp)
            Td850, Td700 = Td_req[0], Td_req[1]
            t850_m, t500_m = T850.magnitude, T500.magnitude
            td850_m, t700_m, td700_m = Td850.magnitude, T700.magnitude, Td700.magnitude
            k_val_mag = (t850_m - t500_m) + td850_m - (t700_m - td700_m)
            k_val = k_val_mag * units.delta_degC
            indices['K Index'] = f'K Index: {k_val.to("delta_degC").magnitude:.2f} °C'
            print("K-Index calculated successfully.")
        except Exception as e:
            indices['K Index'] = 'K Index: N/A'
            print(f"Could not calculate K-Index. Error: {e}")
            
        # Total Totals Index
        try:
            tt_val = mpcalc.total_totals_index(p, T, Td)
            indices['Total Totals'] = f'Total Totals: {tt_val.to("delta_degC").magnitude:.2f} °C'
        except Exception:
            indices['Total Totals'] = 'Total Totals: N/A'

        # Precipitable Water (mm)
        try:
            pwat_val = mpcalc.precipitable_water(p, Td)
            indices['Precipitable Water'] = f'Precipitable Water: {pwat_val.to("mm").magnitude:.2f} mm'
        except Exception:
            indices['Precipitable Water'] = 'Precipitable Water: N/A'

        # 0-6 km Bulk Shear
        try:
            shear_u, shear_v = mpcalc.bulk_shear(p, u, v, height=height, depth=6000 * units.meter)
            shear_mag = mpcalc.wind_speed(shear_u, shear_v)
            indices['0-6km Bulk Shear'] = f'0-6km Bulk Shear: {shear_mag.to("knots").magnitude:.2f} kts'
        except Exception:
            indices['0-6km Bulk Shear'] = '0-6km Bulk Shear: N/A'

        # --- 5. Create the Skew-T Plot ---
        print("Generating Skew-T plot...")
        fig = plt.figure(figsize=(12, 12)) 
        skew = SkewT(fig, rotation=45)

        skew.plot(p, T, 'r', linewidth=2, label='Temperature')
        skew.plot(p, Td, 'g', linewidth=2, label='Dewpoint')
        # Every level's wind barb, except for high-resolution soundings (thousands of levels), which
        # get one barb per 25 hPa
        if len(p) > 100:
            idx = np.unique(mpcalc.resample_nn_1d(p.m, np.arange(1000, 99, -25)))
            skew.plot_barbs(p[idx], u[idx], v[idx], y_clip_radius=0.03)
        else:
            skew.plot_barbs(p, u, v, y_clip_radius=0.03)
        skew.plot(p, parcel_prof, 'k', linewidth=2, linestyle='--', label='Parcel Path')

        skew.shade_cin(p, T, parcel_prof, Td, alpha=0.2, color='blue')
        skew.shade_cape(p, T, parcel_prof, alpha=0.2, color='red')
        
        if np.isfinite(lcl_pressure):
            label_lcl = f'LCL: {lcl_pressure.m:.0f} hPa, {lcl_height.to("m").m:.0f} m'
            skew.plot(lcl_pressure, lcl_temperature, 'ko', markerfacecolor='black', label=label_lcl)
        if np.isfinite(lfc_pressure):
            label_lfc = f'LFC: {lfc_pressure.m:.0f} hPa, {lfc_height.to("m").m:.0f} m'
            skew.plot(lfc_pressure, lfc_temperature, 'bo', markerfacecolor='blue', label=label_lfc)
        if np.isfinite(el_pressure):
            label_el = f'EL: {el_pressure.m:.0f} hPa, {el_height.to("m").m:.0f} m'
            skew.plot(el_pressure, el_temperature, 'ro', markerfacecolor='red', label=label_el)

        skew.plot_dry_adiabats(alpha=0.25)
        skew.plot_moist_adiabats(alpha=0.25)
        skew.plot_mixing_lines(alpha=0.25)

        skew.ax.axvline(0, color='c', linestyle='--', linewidth=2)

        # --- 6. Finalize the Plot ---
        skew.ax.set_ylim(1050, 100)
        skew.ax.set_xlim(-40, 40)
        
        plt.title(f'Skew-T Log-P for {station_name_long}', loc='left')
        plt.title(f'Valid: {time_val:%Y-%m-%d %H:%M}Z', loc='right')
        plt.xlabel(f'Temperature ({T.units:~P})')
        plt.ylabel(f'Pressure ({p.units:~P})')
        skew.ax.tick_params(axis='x', colors='black')
        skew.ax.tick_params(axis='y', colors='black')
        
        # --- 6a. Add Indices to Plot ---
        indices_text = (
            f'CAPE: {cape.magnitude:.2f} J/kg\n'
            f'CIN: {cin.magnitude:.2f} J/kg\n'
            f'{indices.get("Lifted Index", "Lifted Index: N/A")}\n'
            f'{indices.get("Showalter Index", "Showalter Index: N/A")}\n'
            f'{indices.get("K Index", "K Index: N/A")}\n'
            f'{indices.get("Total Totals", "Total Totals: N/A")}\n'
            f'{indices.get("Precipitable Water", "Precipitable Water: N/A")}\n'
            f'{indices.get("0-6km Bulk Shear", "0-6km Bulk Shear: N/A")}'
        )
        
        # Place text box in the upper-left corner of the plot axes
        skew.ax.text(0.02, 0.98, indices_text,
                     transform=skew.ax.transAxes,
                     verticalalignment='top',
                     horizontalalignment='left',
                     fontsize=10,
                     bbox=dict(boxstyle='round', facecolor='white', alpha=0.75))

        skew.ax.legend()
        plt.grid(True)

        # --- 7. Save the Plot ---
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        plt.savefig(filepath, bbox_inches='tight')
        plt.close(fig)
        print(f"Plot saved to: {filepath}")

    except Exception as e:
        # This will catch errors in plotting or data processing *after* a successful download
        print(f"An error occurred during plot generation: {e}")


# --- Main execution ---
SAVE_DIR = '/home/dimitris/weather_station/upper_air_soundings'
# Without an observed Thessaloniki sounding for this long, also plot the fallbacks.
# The dashboard (timeseries_plotly_optimised.py, SOUNDING_FALLBACK_AFTER_HOURS) uses the same rule.
FALLBACK_AFTER_HOURS = 36
THESSALONIKI_LAT, THESSALONIKI_LON, THESSALONIKI_ELEVATION_M = 40.52, 22.97, 4
LARISSA_LAT, LARISSA_LON, LARISSA_ELEVATION_M = 39.65, 22.47, 73   # Larissa airport (LGLR), 107 km SW
LARISSA_MODEL_KEEP = 8   # Larissa's GFS plots (one per 6 h) are pruned to the newest 8; soundings are all kept


def plot_model_profile(file_id, place, lat, lon, elevation_m, slot, save_dir):
    """Plots the GFS profile at `slot` as <file_id>_model_<YYYYmmdd_HHMM>.png unless it already exists."""
    path = os.path.join(save_dir, f"{file_id}_model_{slot:%Y%m%d_%H%M}.png")
    if os.path.exists(path):
        print(f"GFS profile for {place} at {slot:%Y-%m-%d %H}Z is already plotted.")
        return
    try:
        df = fetch_model_profile(slot, lat, lon, elevation_m)
        plot_sounding(df, f'{place} (GFS model, not observed)', slot, path)
    except Exception as e:
        print(f"GFS profile for {place} not available: {type(e).__name__}: {e}")


def prune_plots(save_dir, pattern, keep):
    """Deletes all but the newest `keep` plots matching pattern (their names end in the valid time)."""
    for path in sorted(glob.glob(os.path.join(save_dir, pattern)))[:-keep]:
        os.remove(path)
        print(f"Deleted old plot {os.path.basename(path)}")


def main(save_dir=SAVE_DIR):
    now_utc = datetime.now(timezone.utc)
    slot = now_utc.replace(hour=now_utc.hour - now_utc.hour % 6, minute=0, second=0, microsecond=0)

    # 1. Always look for the real Thessaloniki airport sounding first (WMO 16622)
    thessaloniki_dt = plot_skewt_for_station('16622', 'Thessaloniki International Airport (LGTS)', save_dir)

    # 2. Athens Hellinikon (16716), 301 km S, usually launches at 12Z
    plot_skewt_for_station('16716', 'Athens Hellinikon (16716), 301 km S of Thessaloniki', save_dir)

    # 3. Larissa has no radiosonde in the WMO network (not in NOAA's IGRA2 list, nothing at Wyoming for
    #    16648), so plot the GFS profile for Larissa airport at the latest 6-hourly time instead. The file
    #    name uses Larissa's WMO surface-station number, 16648.
    plot_model_profile('16648', 'Larissa', LARISSA_LAT, LARISSA_LON, LARISSA_ELEVATION_M, slot, save_dir)
    prune_plots(save_dir, "16648_model_*.png", LARISSA_MODEL_KEEP)

    if thessaloniki_dt is not None and now_utc - thessaloniki_dt <= timedelta(hours=FALLBACK_AFTER_HOURS):
        return

    print(f"No observed Thessaloniki sounding in the last {FALLBACK_AFTER_HOURS} h: "
          f"plotting the GFS model profile for Thessaloniki and the nearest observed sounding (Sofia).")

    # 4. GFS model profile for Thessaloniki airport at the latest 6-hourly time
    plot_model_profile('16622', 'Thessaloniki', THESSALONIKI_LAT, THESSALONIKI_LON, THESSALONIKI_ELEVATION_M,
                       slot, save_dir)

    # 5. Nearest radiosonde station that still reports: Sofia, 239 km north
    plot_skewt_for_station('15614', 'Sofia (15614), 239 km N of Thessaloniki', save_dir)


if __name__ == '__main__':
    main()
