#!/bin/bash

#==============================================================================
# VERSION 5: Download the newest image the server already has. Images appear
#            about 30 minutes after their nominal time, so try the newest
#            quarter-hour first and step back, instead of always asking for
#            the one from 45 minutes ago (VERSION 4). The downloaded timestamp
#            is passed on so the stamp on the image matches it.
#==============================================================================

# --- Configuration ---
BASE_URL_PREFIX="https://imn-rust-lb.infoplaza.io/v4/nowcast/tiles/satellite-europe/"
TILE_INFO="/7/46/69/52/76?outputtype=jpeg"
OUTPUT_DIR="/home/dimitris/weather_station/satellite_latest"
OUTPUT_FILE="satellite_greece.jpg"
OUTPUT_PATH="$OUTPUT_DIR/$OUTPUT_FILE"
TMP_PATH="$OUTPUT_DIR/.download.jpg"            # partial downloads never replace the current image
LAST_FILE="$OUTPUT_DIR/.last_timestamp"         # newest image already processed
MAX_AGE_MIN=90                                  # give up looking further back than this

# --- End Configuration ---

mkdir -p "$OUTPUT_DIR"
echo "Starting satellite image download loop..."

while true; do
    echo "-----------------------------------------------------"
    echo "$(date): Looking for the newest satellite image."

    # --- Step 1: Find and download the newest available image ---
    LATEST_TIMESTAMP=""
    for AGO in $(seq 0 15 $MAX_AGE_MIN); do
        T=$(date -u -d "$AGO minutes ago" +%Y%m%d%H%M)
        CANDIDATE="${T:0:10}$(printf "%02d" $(( (10#${T:10:2} / 15) * 15 )))"
        if curl -f -L -s -o "$TMP_PATH" "${BASE_URL_PREFIX}${CANDIDATE}${TILE_INFO}" && [ -s "$TMP_PATH" ]; then
            LATEST_TIMESTAMP="$CANDIDATE"
            break
        fi
    done

    # --- Step 2: Process it, unless it is the one already on the dashboard ---
    if [ -z "$LATEST_TIMESTAMP" ]; then
        echo "$(date): No image found for the last ${MAX_AGE_MIN} minutes. Check URL or network."
        rm -f "$TMP_PATH"
    elif [ "$LATEST_TIMESTAMP" = "$(cat "$LAST_FILE" 2>/dev/null)" ]; then
        echo "$(date): Newest image (${LATEST_TIMESTAMP} UTC) is already processed."
        rm -f "$TMP_PATH"
    else
        mv "$TMP_PATH" "$OUTPUT_PATH"
        echo "$(date): Downloaded image for ${LATEST_TIMESTAMP} UTC. Processing image..."
        if python /home/dimitris/weather_station/satellite_latest/pre-process_image.py "$LATEST_TIMESTAMP" \
            && python /home/dimitris/weather_station/satellite_latest/process_image.py "$LATEST_TIMESTAMP"; then
            echo "$LATEST_TIMESTAMP" > "$LAST_FILE"
        else
            echo "$(date): Processing failed; it will be retried next cycle."
        fi
    fi

    # --- Step 3: Wait for the Next Cycle ---
    echo "$(date): Waiting 15 minutes (900 seconds) before the next download..."
    sleep 900
done
