import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import os
import numpy as np
import cv2
import hailo
from playsound import playsound
import threading
import socket
import csv
import paho.mqtt.client as mqtt
import time

from hailo_apps_infra.hailo_rpi_common import (
    get_caps_from_pad,
    get_numpy_from_buffer,
    app_callback_class,
)
from hailo_apps_infra.detection_pipeline import GStreamerDetectionApp

barcode_count = 0
summary_count = 0
track_id_list = set()
previous_frame = None
unblock_timestamp = 0
last_published_message = None
last_summary_increment_time = 0

STARTUP_IGNORE_SECONDS = 2.0
startup_time = time.time()

counter_enabled = False

# MQTT setup
broker_address = "192.168.5.2"
topic = "barcode/data"
mqtt_client = mqtt.Client()
try:
    mqtt_client.connect(broker_address, port=1883, keepalive=60)
    mqtt_client.loop_start()   # <-- REQUIRED
    print("MQTT connected & loop started")
except Exception as e:
    print(f"MQTT FAILED TO CONNECT: {e}")


def start_server_socket(host='127.0.0.1', port=12345, shared_data=None):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(1)
    print(f"Socket server started at {host}:{port}", flush=True)
    try:
        while True:
            client_socket, client_address = server_socket.accept()
            print(f"Client connected from {client_address}")
            while True:
                data = client_socket.recv(1024)
                if not data:
                    print(f"Client {client_address} disconnected")
                    break
                received_message = data.decode()
                if shared_data is not None:
                    shared_data["client_data"] = received_message
                    shared_data["last_received_time"] = time.time()
    except Exception as e:
        print(f"Socket server error: {e}")
    finally:
        server_socket.close()


def monitor_barcode_count(shared_data, interval=10):
    global barcode_count
    log_file = "/home/pi/hailo-rpi5-examples/basic_pipelines/detectionlog.txt"
    audio_file = "/home/pi/hailo-rpi5-examples/basic_pipelines/audios/MismatchNumber.wav"
    while True:
        time.sleep(interval)
        last_received_time = shared_data.get("last_received_time")
        client_data = shared_data.get("client_data")
        if last_received_time is not None and client_data is not None:
            elapsed_time = time.time() - last_received_time
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            if elapsed_time > interval:
                if barcode_count > int(client_data):
                    message = f"num:{client_data}------{timestamp} - Mismatch numbers\n"
                    try:
                        barcode_count = 0
                        track_id_list.clear()
                        playsound(audio_file)
                    except Exception as e:
                        print(f"Error playing audio: {e}")
                    try:
                        with open(log_file, "a") as f:
                            f.write(message)
                    except Exception as e:
                        print(f"Error writing to log file: {e}")
                else:
                    barcode_count = 0
                    track_id_list.clear()


def motion_detection(current_frame):
    global previous_frame
    gray_frame = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
    blurred_frame = cv2.GaussianBlur(gray_frame, (3, 3), 0)
    if previous_frame is None:
        previous_frame = blurred_frame
        return False, 0
    frame_diff = cv2.absdiff(previous_frame, blurred_frame)
    _, thresholded = cv2.threshold(frame_diff, 25, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresholded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    motion_detected = any(cv2.contourArea(c) > 150000 for c in contours)
    previous_frame = blurred_frame
    return motion_detected, 0


class user_app_callback_class(app_callback_class):
    def __init__(self, shared_data, pixel_threshold=10, block_percentage=0.70):
        super().__init__()
        self.use_frame = True
        self.shared_data = shared_data
        self.pixel_threshold = pixel_threshold
        self.block_percentage = block_percentage

        # --- Event state flags ---
        self.was_blocked = False
        self.was_no_barcode = False
        self.was_barcode_detected = False
        self.was_blocked_barcode = False

        self.frame = None
        self.audio_thread = None
        self.motion_detected_time = None
        self.blocked_detected_time = None

    def is_camera_fully_blocked(self, frame):
        if frame is None:
            return True
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        avg_brightness = np.mean(frame)
        return avg_brightness < 20

    def play_audio(self, file_path):
        if self.audio_thread and self.audio_thread.is_alive():
            return
        def audio_task():   
            try:
                abt_path = "/home/pi/hailo-rpi5-examples/basic_pipelines/audios/"
                playsound(abt_path + file_path) 
            except Exception as e:
                print(f"Error playing audio: {e}")
        self.audio_thread = threading.Thread(target=audio_task)
        self.audio_thread.start()


def app_callback(pad, info, user_data):
    global barcode_count, summary_count, unblock_timestamp, last_published_message, last_summary_increment_time
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK

    user_data.increment()
    format, width, height = get_caps_from_pad(pad)
    frame = get_numpy_from_buffer(buffer, format, width, height) if format and width and height else None
    detections = hailo.get_roi_from_buffer(buffer).get_objects_typed(hailo.HAILO_DETECTION)
    current_time = time.time()
    camera_blocked = user_data.is_camera_fully_blocked(frame)

    # --- Camera Blocked ---
    if camera_blocked:
        if not user_data.was_blocked:
            message = "blocked camera detected"
            if message != last_published_message:
                mqtt_client.publish(topic, message)
                print(f"Published to MQTT: {message}")
                last_published_message = message
            #user_data.play_audio("CameraBlocked.wav")
            print("Camera is blocked. Skipping frame processing.\n")
            user_data.was_blocked = True
        unblock_timestamp = 0
        return Gst.PadProbeReturn.OK
    else:
        if user_data.was_blocked:
            user_data.was_blocked = False

    # --- Motion / No Barcode ---
    if not detections:
        if time.time() - startup_time < STARTUP_IGNORE_SECONDS:
            return Gst.PadProbeReturn.OK

        motion, _ = motion_detection(frame)
        if motion:
            if not user_data.was_no_barcode:
                message = "No Barcode Detected"
                if message != last_published_message:
                    mqtt_client.publish(topic, message)
                    print(f"Published to MQTT: {message}")
                    last_published_message = message
                #user_data.play_audio("Nobarcode.wav")
                print("Motion detected, playing motion audio.\n")
                user_data.was_no_barcode = True
        else:
            user_data.was_no_barcode = False
    
    # --- Barcode Detected ---
    blocked_barcode_detected = False
    for detection in detections:
        label = detection.get_label()
        confidence = detection.get_confidence()

        if label == "have_barcode" and confidence > 0.85:
            try:
                track = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
                track_id = track[0].get_id() if len(track) == 1 else f"no_track_{hash(str(detection.get_bbox()))}"
            except:
                track_id = f"no_track_{hash(str(detection.get_bbox()))}"

            if track_id not in track_id_list:
                track_id_list.add(track_id)
                barcode_count = len(track_id_list)

                if current_time - last_summary_increment_time > 3:
                    if counter_enabled:
                        summary_count += 1
                        last_summary_increment_time = current_time
                        mqtt_client.publish("summary/data", str(summary_count))
                        print(f"[Summary] Count updated: {summary_count}")

                if not user_data.was_barcode_detected:
                    message = "Barcode Detected"
                    mqtt_client.publish(topic, message)
                    print(f"Published to MQTT: {message}")
                    #user_data.play_audio("BarcodeDetected.wav")
                    user_data.was_barcode_detected = True
        elif label == "blocked_barcode" and confidence > 0.8:
            blocked_barcode_detected = True
            if not user_data.was_blocked_barcode:
                message = "blocked barcode detected"
                mqtt_client.publish(topic, message)
                print(f"Published to MQTT: {message}")
                #user_data.play_audio("BlockedBarcode.wav")
                user_data.was_blocked_barcode = True

    if not blocked_barcode_detected:
        user_data.was_blocked_barcode = False
    if not detections:
        user_data.was_barcode_detected = False

    return Gst.PadProbeReturn.OK

def on_mqtt_message(client, userdata, msg):
    global summary_count, barcode_count, track_id_list
    global last_summary_increment_time, counter_enabled

    topic = msg.topic
    payload = msg.payload.decode().strip()

    # 🔐 COUNTER CONTROL
    if topic == "counter/control":
        if payload == "START":
            counter_enabled = True
            print("[CONTROL] Counter ENABLED")
        elif payload == "STOP":
            counter_enabled = False
            print("[CONTROL] Counter PAUSED")
        return

    # 🔁 CALIBRATION COMMAND (NEW)
    if topic == "summary/calibrate":
        try:
            new_value = int(payload)
            summary_count = new_value
            barcode_count = new_value
            track_id_list.clear()
            last_summary_increment_time = time.time()

            client.publish("summary/data", str(summary_count))
            print(f"[CALIBRATE] Summary set to {summary_count}")

        except ValueError:
            print("Invalid calibrate payload:", payload)

        return

    # 🔄 EXISTING RESET
    if topic == "summary/reset" and payload == "RESET":
        print("[MQTT] Summary reset received")

        summary_count = 0
        barcode_count = 0
        track_id_list.clear()
        last_summary_increment_time = 0

        client.publish("summary/data", "0")

mqtt_client.on_message = on_mqtt_message

# mqtt sbscribe for summary reset and calibrate
mqtt_client.subscribe("summary/reset")
mqtt_client.subscribe("summary/calibrate")
mqtt_client.subscribe("counter/control")

if __name__ == "__main__":
    shared_data = {"client_data": None, "last_received_time": None}
    user_data = user_app_callback_class(shared_data, pixel_threshold=10, block_percentage=70)

    socket_thread = threading.Thread(target=start_server_socket, args=('127.0.0.1', 12345, shared_data), daemon=True)
    socket_thread.start()

    monitor_thread = threading.Thread(target=monitor_barcode_count, args=(shared_data,), daemon=True)
    monitor_thread.start()

    app = GStreamerDetectionApp(app_callback, user_data)

    try:
        app.run()
    except KeyboardInterrupt:
        print("Publisher stopped.")
        mqtt_client.disconnect()
