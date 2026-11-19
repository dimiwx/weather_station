#!/bin/bash

# This script automates launching all necessary scripts for the weather station,
# each in its own separate terminal window. This is ideal for scripts that need
# to run continuously in the background.
#
# NOTE: This script uses 'gnome-terminal'. If you use a different terminal
# emulator (like konsole, xterm, terminator), you will need to replace
# 'gnome-terminal' with the appropriate command for your system.

echo "--- Launching Weather Station Scripts in New Terminals ---"

echo "Launching wind data script..."
gnome-terminal --title="Wind Data" -- /bin/bash -c "python wind.py; exec /bin/bash"

echo "Launching ESP32 USB logger..."
gnome-terminal --title="ESP32 Logger" -- /bin/bash -c "bash auto_usb_logger.sh; exec /bin/bash"

echo "Launching air quality script..."
gnome-terminal --title="Air Quality" -- /bin/bash -c "python air_quality.py; exec /bin/bash"

echo "Launching rainfall script..."
gnome-terminal --title="Rainfall" -- /bin/bash -c "python rainfall2.py; exec /bin/bash"

echo "Launching lightning detection script..."
gnome-terminal --title="Lightning Detection" -- /bin/bash -c "bash auto_lightning.sh; exec /bin/bash"

echo "Launching camera capture script..."
gnome-terminal --title="Camera Capture" -- /bin/bash -c "bash camera_capture.sh; exec /bin/bash"

echo "Launching satellite imagery download..."
gnome-terminal --title="Satellite Download" -- /bin/bash -c "bash download_satellite2.sh; exec /bin/bash"

echo "Launching Weather Underground upload script..."
gnome-terminal --title="WU Upload" -- /bin/bash -c "python upload_WU.py; exec /bin/bash"



echo "Launching ngrok tunnel..."
gnome-terminal --title="Ngrok Tunnel" -- /bin/bash -c "ngrok http --url=weather-krini.ngrok.io 8050; exec /bin/bash"

cd weather_station

echo "Launching Plotly graph generator..."
gnome-terminal --title="Plotly Graphs" -- /bin/bash -c "python auto_plotly.py; exec /bin/bash"

echo "Launching Telegram notification script..."
gnome-terminal --title="Telegram Notifier" -- /bin/bash -c "bash auto_telegram.sh; exec /bin/bash"

echo "--- All scripts have been launched in separate terminals. ---"


wait
