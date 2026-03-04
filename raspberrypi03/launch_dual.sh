#!/bin/bash

sleep 5

# --- GLOBAL WAIT FOR NETWORK (Pi4 MQTT broker) ---
# Wait until broker 192.168.5.2 responds
echo "Waiting for MQTT broker (192.168.5.2)..."
while ! ping -c1 -W1 192.168.5.2 >/dev/null 2>&1; do
    sleep 1
done
echo "MQTT broker reachable. Starting programs..."

sleep 3  # short buffer

# --- Terminal 1: Barcode Detection ---
lxterminal -t "Barcode Detection" --command="bash -c '
cd /home/pi &&
source /home/pi/shortcut.sh &&
python basic_pipelines/barcode_detection_summary.py -i rpi --hef-path yolov8n2try.hef --labels labels.json;
exec bash'" &

sleep 10  # ensure barcode has time to initialize its processing + MQTT

# --- Terminal 2: Integration GUI ---
lxterminal -t "Integration Summary GUI" --command="bash -c '
cd /home/pi/hailo-rpi5-examples &&
source /home/pi/shortcut.sh &&
python3 Integration25_TEST.py --run;
exec bash'" &
