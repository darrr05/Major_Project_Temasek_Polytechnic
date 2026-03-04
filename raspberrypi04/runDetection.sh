#!/bin/bash

# Terminal 1: Hailo Dome Detection
# lxterminal -t "Hailo Dome Detection" --command='bash -c \"\
# cd /home/pi/hailo-rpi5-examples && \
# source setup_env.sh && \
# export PYTHONIOENCODING=utf-8 LANG=C.UTF-8 LC_ALL=C.UTF-8 && \
# python3 basic_pipelines/DomeCamMQTT_SaveImg_hailo_test.py --hef-path resources/yolov8s_v9.hef --labels-json resources/Object_Detection_labels.json --input rtsp://admin:ILoveKumbar1@192.168.1.64:554/Streaming/Channels/101/; \
# exec bash\""

lxterminal -t "YOLO Object Detection" --command="bash -c \"\
source /home/pi/yoloobject/bin/activate && \
cd /home/pi/Object\ Detection && \
python DomeCamMQTT_SaveImg_test.py; \
exec bash\""
