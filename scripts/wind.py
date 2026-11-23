#!/usr/bin/env python3
import minimalmodbus
import serial
import time
import csv
import os
from datetime import datetime

def setup_instrument(port, slave_address=1, baudrate=4800):
    """Initialize a Modbus instrument on the given port."""
    instrument = minimalmodbus.Instrument(port, slave_address)
    instrument.serial.baudrate = baudrate
    instrument.serial.bytesize = 8
    instrument.serial.parity   = serial.PARITY_NONE
    instrument.serial.stopbits = 1
    instrument.serial.timeout  = 1  # seconds
    instrument.mode = minimalmodbus.MODE_RTU
    return instrument

def main():
    # Physical USB sockets (by-path), not /dev/ttyUSB* numbers: those swap between boots.
    anemometer_port = '/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-port0'  # Anemometer: wind speed sensor
    wind_vane_port   = '/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0-port0'  # Wind vane: wind direction sensor

    # Set up instruments
    anemometer = setup_instrument(anemometer_port)
    wind_vane  = setup_instrument(wind_vane_port)

    # CSV file path
    data_file = "/home/dimitris/weather_station/weather_station_data.csv"
    # Ensure directory exists
    os.makedirs(os.path.dirname(data_file), exist_ok=True)

    # If the file doesn't exist, create it and write a header row (optional)
    if not os.path.exists(data_file):
        with open(data_file, mode='w', newline='') as f:
            writer = csv.writer(f)
            # Header: Column 1 is Timestamp, columns 2-11 are empty placeholders,
            # column 12: Wind Speed, column 13: Wind Direction
            header = ["Timestamp"] + [""] * 10 + ["Wind Speed", "Wind Direction"]
            writer.writerow(header)

    print("Starting sensor polling and logging every 31 seconds. Press Ctrl+C to exit.\n")

    while True:
        try:
            # Read from the anemometer: register 0 holds wind speed (value is 10x actual m/s)
            raw_speed = anemometer.read_register(0, 0)
            wind_speed = raw_speed / 10.0

            # Read from the wind vane: register 1 holds wind direction in degrees
            raw_degree = wind_vane.read_register(1, 0)
            # Calibrate wind direction: -90° for how the vane is mounted, then +23° from comparing
            # 79 windy hours (17-31 Oct 2026) with the Open-Meteo model, which showed the station
            # reading a consistent 23° counter-clockwise. Applied from 2026-10-31; older rows lack it.
            calibrated_degree = (raw_degree - 90 + 23) % 360

            # Get the current timestamp (UTC e.g., "2024-05-20 12:00:00")
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

            # Prepare a row with 13 columns:
            # Column 1: timestamp, columns 2-11: empty, column 12: wind_speed, column 13: calibrated_degree
            row = [timestamp] + [""] * 10 + [wind_speed, calibrated_degree]

            # Append the row to the CSV file
            with open(data_file, mode='a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(row)

            print(f"{timestamp} -> Wind Speed: {wind_speed:.1f}, Wind Direction: {calibrated_degree}")
        except Exception as e:
            print(f"Error reading sensors or writing file: {e}")

        time.sleep(31)

if __name__ == "__main__":
    main()
