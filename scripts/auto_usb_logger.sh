#!/bin/bash
# Keeps the ESP32 USB logger running: whenever usb_weather_logger_dual.py exits, it starts again after 10 s.
# Ctrl-C (or closing this window) stops the loop.
# No need to stop it for Thonny: the logger leaves a board's port alone while another program has it open.
cd /home/dimitris || exit 1
trap 'echo "USB logger loop stopped."; exit 0' INT TERM
while true; do
    echo "$(date '+%F %T') Starting usb_weather_logger_dual.py"
    python3 usb_weather_logger_dual.py
    echo "$(date '+%F %T') usb_weather_logger_dual.py exited with code $?; restarting in 10 s (Ctrl-C stops the loop)"
    sleep 10
done
