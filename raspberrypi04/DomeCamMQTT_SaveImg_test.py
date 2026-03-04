import cv2
import torch
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFont
import numpy as np
from ultralytics import YOLO
import threading
import time
import paho.mqtt.client as mqtt
import json
import os
import sys

torch.set_num_threads(4)
torch.set_num_interop_threads(2)

# ===================== PERFORMANCE TUNING =====================

FRAME_SKIP = 2        # Try 2 or 3 (3 = ~3x faster)
frame_counter = 0

last_detection_result = None   # (frame, count, save_frame)

incident_active = False

# ===================== CONFIG & CONSTANTS =====================

ROI_JSON_PATH = "Settings.json"

MQTT_BROKER = "192.168.5.2"
MQTT_PORT = 1883
MQTT_PUBLISH_TOPIC = "counter/data"
MQTT_SUBSCRIBE_MISMATCH_TOPIC = "mismatch/data"
MQTT_SUBSCRIBE_BARCODE_TOPIC = "barcode/data"
MQTT_PAYMENT_RESET_TOPIC = "payment/reset"
MQTT_CLIENT_ID = "YOLO_Detector_Client_" + str(time.time())

RTSP_URL = "rtsp://admin:ILoveKumbar1@192.168.1.64:554/Streaming/Channels/101/"

MODEL_PATH = "yolov12n_Object Detection_v14.onnx"

BARCODE_BLOCKED_DIR = "/media/pi/Barcode Blocked Images"
CAMERA_BLOCKED_DIR  = "/media/pi/Camera Blocked Images"
MISMATCH_DIR = "/media/pi/Mismatch Images"

# ===================== GLOBAL STATE =====================

latest_dome_frame = None           # Full-resolution RTSP frame
frame_lock = threading.Lock()
saved_frame_lock = threading.Lock()

running = True
roi = None
roi_set = False
detection_started = False
drawing = False
ix = iy = fx = fy = -1
frame_copy = None
original_frame_copy = None
display_frame = None               # Resized frame used in GUI

mqtt_client = None
mqtt_connected = False
last_published_count = -1

# This will hold the latest GUI frame AFTER detection + blackout
saved_gui_frame = None

HAND_OBJECT_CONF_THRESHOLD = 0.5
PAYMENT_DETECTION_CONF_THRESHOLD = 0.60
PAYMENT_RESET_COOLDOWN = 5.0
last_payment_reset_time = 0

PAYMENT_HOLD_SECONDS = 0     # must detect continuously for 2s before reset

payment_hold_start_time = None # when payment_valid first became True
payment_hold_armed = False     # prevents re-firing while still holding

HOLDING_CONFIRM_SECONDS = 0.2   # must hold for 1 seconds

holding_start_time = None
holding_confirmed = False

device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using device: {device}; model: {MODEL_PATH}")

# ===================== ROI JSON HELPERS =====================

def load_roi_from_json():
    if not os.path.exists(ROI_JSON_PATH):
        return None
    try:
        with open(ROI_JSON_PATH, "r") as f:
            data = json.load(f)
        roi_data = data.get("roiCoordinates", None)
        if roi_data:
            x = int(roi_data.get("x", 0))
            y = int(roi_data.get("y", 0))
            w = int(roi_data.get("width", 0))
            h = int(roi_data.get("height", 0))
            if w > 0 and h > 0:
                return (x, y, x + w, y + h)
    except Exception as e:
        print(f"Error loading ROI from JSON: {e}")
    return None

def save_roi_to_json(x1, y1, x2, y2):
    roi_data = {
        "firstStart": 0,
        "roiCoordinates": {
            "x": int(x1),
            "y": int(y1),
            "width": int(x2 - x1),
            "height": int(y2 - y1)
        }
    }
    try:
        with open(ROI_JSON_PATH, "w") as f:
            json.dump(roi_data, f, indent=2)
        print(f"ROI coordinates saved to {ROI_JSON_PATH}")
    except Exception as e:
        print(f"Error saving ROI to JSON: {e}")

# ===================== YOLO MODEL LOAD =====================

try:
    model = YOLO(MODEL_PATH, task="detect")
except Exception as e:
    print(f"Failed to load YOLO model: {e}")
    raise

# Normalise model.names to a dict: {class_id: class_name}
raw_names = model.names
if isinstance(raw_names, dict):
    names_dict = raw_names
elif isinstance(raw_names, list):
    names_dict = {i: name for i, name in enumerate(raw_names)}
else:
    raise TypeError(f"Unexpected type for model.names: {type(raw_names)}")

# Map model class names (lowercased) to IDs
class_ids = {v.lower(): k for k, v in names_dict.items()}

hand_id = class_ids.get("hand")
object_id = class_ids.get("objects")  # your dataset uses 'objects'
scanner_id = class_ids.get("scanner")
phone_id = class_ids.get("phone")
card_id = class_ids.get("card")
nets_id = class_ids.get("nets_machine")
wallet_id = class_ids.get("wallet")

print("Model classes:", names_dict)
print("Class IDs:", {
    "hand": hand_id,
    "objects": object_id,
    "scanner": scanner_id,
    "phone": phone_id,
    "card": card_id,
    "nets_machine": nets_id,
    "wallet": wallet_id
})

if hand_id is None or object_id is None:
    raise ValueError("Model must contain classes: 'hand' and 'objects'.")


def warm_up_model():
    try:
        dummy_image = np.zeros((832, 832, 3), dtype=np.uint8)
        _ = model.predict(
            dummy_image,
            conf=0.35,
            iou=0.5,
            device=device,
            imgsz=(832, 832),
            max_det=10,
            agnostic_nms=False
        )
        print("Model warm-up done.")
    except Exception as e:
        print(f"Model warm-up failed: {e}")

# ===================== GEOMETRY: HAND HOLDING OBJECT =====================

def is_touching(boxA, boxB, threshold=25):
    """
    Check if two boxes are overlapping or within a small distance.
    threshold expands the object box so near-contact counts as 'holding'.
    """
    (ax1, ay1, ax2, ay2) = boxA
    (bx1, by1, bx2, by2) = boxB

    bx1 -= threshold
    by1 -= threshold
    bx2 += threshold
    by2 += threshold

    return not (ax2 < bx1 or ax1 > bx2 or ay2 < by1 or ay1 > by2)

# ===================== MQTT CALLBACKS =====================

def on_mqtt_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        print("Connected to MQTT broker successfully")
        mqtt_connected = True
        try:
            client.subscribe(MQTT_SUBSCRIBE_MISMATCH_TOPIC)
            client.subscribe(MQTT_SUBSCRIBE_BARCODE_TOPIC)
            client.subscribe("incident/clear")
            print(f"Subscribed to {MQTT_SUBSCRIBE_MISMATCH_TOPIC} and {MQTT_SUBSCRIBE_BARCODE_TOPIC}")
        except Exception as e:
            print(f"Subscribe failed: {e}")
    else:
        print(f"Failed to connect to MQTT broker with code: {rc}")
        mqtt_connected = False

def on_mqtt_disconnect(client, userdata, rc):
    global mqtt_connected
    print(f"Disconnected from MQTT broker with code: {rc}")
    mqtt_connected = False
    if rc != 0:
        print("Unexpected disconnection, will attempt to reconnect...")

def on_mqtt_message(client, userdata, msg):
    global latest_dome_frame, saved_gui_frame
    global incident_active

    try:
        payload = msg.payload.decode().strip()
        pl = payload.lower()
        print(f"MQTT Received: {payload} on topic {msg.topic}")

        # Get latest full frame (backup option)
        with frame_lock:
            frame_full = None if latest_dome_frame is None else latest_dome_frame.copy()

        # Get latest blackout frame (preferred save)
        with saved_frame_lock:
            blackout_frame = None if saved_gui_frame is None else saved_gui_frame.copy()

        # -----------------------------
        # Incident clear
        # -----------------------------
        if msg.topic == "incident/clear":
            if payload.strip().upper() == "CLEAR":
                incident_active = False
            return

        # -----------------------------
        # Mismatch topic: set flag + save
        # -----------------------------
        if msg.topic == MQTT_SUBSCRIBE_MISMATCH_TOPIC:
            if "mismatch detected" in pl:
                incident_active = True
                print("[MQTT] MISMATCH detected - saving dome camera image...")

                save_dir = MISMATCH_DIR
                prefix = "Mismatch_Case"
                os.makedirs(save_dir, exist_ok=True)

                if blackout_frame is not None:
                    img_to_save = blackout_frame
                    print("[SAVE] Using blackout frame for mismatch save.")
                elif frame_full is not None:
                    img_to_save = frame_full
                    print("[SAVE] No blackout frame, using raw full frame.")
                else:
                    print("[SAVE] No frame available - cannot save mismatch.")
                    return

                timestamp = time.strftime("%Y%m%d_%H%M%S")
                screenshot_path = os.path.join(save_dir, f"{prefix}_{timestamp}.jpg")

                ok = cv2.imwrite(screenshot_path, img_to_save)
                print(f"[SAVE] Mismatch save {'OK' if ok else 'FAILED'}: {screenshot_path}")
            return

        # -----------------------------
        # Barcode/camera blocked topic: save
        # -----------------------------
        if msg.topic == MQTT_SUBSCRIBE_BARCODE_TOPIC:
            if pl == "blocked barcode detected":
                save_dir = BARCODE_BLOCKED_DIR
                prefix = "Barcode_Blocked_Case"
                print("[MQTT] BLOCKED barcode received - saving dome camera image...")
            elif pl == "blocked camera detected":
                save_dir = CAMERA_BLOCKED_DIR
                prefix = "Camera_Blocked_Case"
                print("[MQTT] BLOCKED camera received - saving dome camera image...")
            else:
                return

            os.makedirs(save_dir, exist_ok=True)

            if blackout_frame is not None:
                img_to_save = blackout_frame
                print("[SAVE] Using blackout frame for blocked save.")
            elif frame_full is not None:
                img_to_save = frame_full
                print("[SAVE] No blackout frame, using raw full frame.")
            else:
                print("[SAVE] No frame available - cannot save.")
                return

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(save_dir, f"{prefix}_{timestamp}.jpg")

            ok = cv2.imwrite(screenshot_path, img_to_save)
            print(f"[SAVE] Blocked save {'OK' if ok else 'FAILED'}: {screenshot_path}")
            return

    except Exception as e:
        print(f"MQTT message error: {str(e)}")

def start_mqtt_client():
    global mqtt_client, mqtt_connected
    mqtt_client = mqtt.Client(client_id=MQTT_CLIENT_ID)
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_disconnect = on_mqtt_disconnect
    mqtt_client.on_message = on_mqtt_message

    mqtt_connected = False
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print("MQTT client initialized successfully")
    except Exception as e:
        print(f"MQTT connection failed: {e}")
        mqtt_connected = False

# Wait for camera on startup
def wait_for_camera(rtsp_url, retry_interval=2):
    print("[BOOT] Waiting for RTSP camera to become available...")
    while running:
        cap = cv2.VideoCapture(rtsp_url)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None:
                print("[BOOT] RTSP camera is READY.")
                return True
        print("[BOOT] Camera not ready, retrying...")
        time.sleep(retry_interval)
    return False

# ===================== RTSP DOME CAMERA LOOP =====================

# Dome Camera
def dome_camera_loop():
    global latest_dome_frame, running

    while running:
        cap = cv2.VideoCapture(RTSP_URL)

        if not cap.isOpened():
            print("[RTSP] Camera not available, retrying...")
            time.sleep(2)
            continue

        print("[RTSP] Camera connected.")

        fps_input_stream = int(cap.get(cv2.CAP_PROP_FPS) or 0)
        print(f"[RTSP] FPS: {fps_input_stream}")

        while running:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[RTSP] Frame lost, reconnecting...")
                break

            with frame_lock:
                latest_dome_frame = frame.copy()

        cap.release()

# ===================== YOLO DETECTION ON ROI =====================

def run_detection(frame, x1, y1, x2, y2):
    """
    Runs YOLO on the selected region.
    Counts 1 only if 'hand' is touching 'object'.

    For GUI:
      - Draws labels, coloured boxes, yellow 'holding' box.
    For saving:
      - Only blackouts card + nets_machine (no labels/boxes).
    """

    global frame_counter, last_detection_result

    frame_counter += 1

    # Skip YOLO inference on intermediate frames
    if frame_counter % FRAME_SKIP != 0:
        if last_detection_result is not None:
            return last_detection_result


    global mqtt_client, mqtt_connected, last_published_count, last_payment_reset_time

    h, w = frame.shape[:2]
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(1, min(x2, w))
    y2 = max(1, min(y2, h))

    # Make two copies:
    #   frame          -> GUI (with annotations)
    #   save_frame     -> for saving (only blackout)
    save_frame = frame.copy()

    region_gui = frame[y1:y2, x1:x2]
    region_save = save_frame[y1:y2, x1:x2]

    if region_gui.size == 0 or region_save.size == 0:
        return frame, 0, save_frame

    roi_h = y2 - y1
    roi_w = x2 - x1
    roi_size = max(roi_h, roi_w)

    if roi_size < 500:
        imgsz = 640
    else:
        imgsz = 832

    try:
        results = model.predict(
            region_gui,
            conf=0.35,
            iou=0.5,
            device=device,
            imgsz=imgsz,    # <-- CHANGE HERE (much faster on Pi)
            max_det=10,
            classes=[hand_id, object_id, scanner_id, phone_id, card_id, nets_id, wallet_id],
            agnostic_nms=False
        )[0]
    except Exception as e:
        print(f"Detection error: {e}")
        return frame, 0, save_frame


    hand_boxes = []
    object_boxes = []
    BLACKOUT_CONF_THRESHOLD = 0.45  # 50% confidence needed to blackout

    has_hand = False
    has_nets = False
    has_card_or_phone = False

    # --- Card filtering (reduce false blackout) ---
    CARD_FORCE_BLACKOUT_CONF = 0.8   # if YOLO is very confident it's a card -> always blackout
    CARD_MIN_BLACKOUT_CONF   = 0.45   # minimum conf to consider blackout (with shape checks)

    def box_area(box_xyxy):
        x1, y1, x2, y2 = box_xyxy
        return max(1, (x2 - x1)) * max(1, (y2 - y1))

    def is_card_like(box_xyxy):
        x1, y1, x2, y2 = box_xyxy
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        ar = w / h
        # Cards tend to be landscape-ish. Give some tolerance.
        return 1.25 <= ar <= 2.2

    def card_size_ok(box_xyxy, roi_w, roi_h):
        # Prevent huge objects being treated as "card"
        a = box_area(box_xyxy)
        roi_area = max(1, roi_w * roi_h)
        frac = a / roi_area
        # Tune this: typical card shouldn’t take up too much of ROI
        return 0.001 <= frac <= 0.12    

    # Draw optional debug boxes (green for hand, blue for object) on GUI only
    for box in results.boxes:
        cls = int(box.cls[0])
        conf = float(box.conf[0])
        bx1, by1, bx2, by2 = map(int, box.xyxy[0])

        # ---- Get class name + confidence label ----
        class_name = names_dict.get(cls, f"class_{cls}")
        label = f"{class_name} {conf*100:.1f}%"

        # Track payment-related detections
        if cls == hand_id and conf >= PAYMENT_DETECTION_CONF_THRESHOLD:
            has_hand = True
        if cls == nets_id and conf >= PAYMENT_DETECTION_CONF_THRESHOLD:
            has_nets = True
        if (cls == card_id or cls == phone_id) and conf >= PAYMENT_DETECTION_CONF_THRESHOLD:
            has_card_or_phone = True

        # ---- Blackout card + NETS machine in BOTH GUI and save frames ----
        if cls == nets_id:
            if conf >= BLACKOUT_CONF_THRESHOLD:
                cv2.rectangle(region_gui, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
                cv2.rectangle(region_save, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
                continue

        # ---- Blackout CARD only if it's really card-like OR very high confidence ----
        if cls == card_id:
            cbox = (bx1, by1, bx2, by2)

            force_blackout = conf >= CARD_FORCE_BLACKOUT_CONF
            likely_card = (
                conf >= CARD_MIN_BLACKOUT_CONF
                and is_card_like(cbox)
                and card_size_ok(cbox, roi_w, roi_h)
            )

            if force_blackout or likely_card:
                # Optional: only show text on GUI
                class_name = names_dict.get(cls, f"class_{cls}")
                label = f"{class_name} {conf*100:.1f}%"
                cv2.putText(region_gui, label, (bx1, max(0, by1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

                cv2.rectangle(region_gui, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
                cv2.rectangle(region_save, (bx1, by1), (bx2, by2), (0, 0, 0), -1)
                continue
            else:
                # NOT a real-looking card -> treat it like an object (don’t blackout)
                cls = object_id
                # (fall through to normal drawing + counting)

        # ---- Ignore scanner + wallet + phone for everything ----
        if cls in (scanner_id, wallet_id, phone_id):
            continue

        # ---- Draw bounding box for every other class (GUI only) ----
        color = (0, 255, 0) if cls == hand_id else (255, 0, 0)
        cv2.rectangle(region_gui, (bx1, by1), (bx2, by2), color, 2)

        # ---- Draw label text above the box (GUI only) ----
        cv2.putText(
            region_gui,
            label,
            (bx1, max(0, by1 - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 0),
            2
        )

        # ---- Track hand + object for counting ----
        if cls == hand_id:
            hand_boxes.append((bx1, by1, bx2, by2))
        elif cls == object_id:
            object_boxes.append((bx1, by1, bx2, by2))

    # ========== PAYMENT DETECTION WITH PROXIMITY ==========
    current_time = time.time()
    payment_valid = False

    # REQUIRE: hand + nets + (card OR phone)
    if has_hand and has_nets and has_card_or_phone:

        for h in results.boxes:
            cls_h = int(h.cls[0])
            conf_h = float(h.conf[0])
            if cls_h != hand_id or conf_h < PAYMENT_DETECTION_CONF_THRESHOLD:
                continue

            hx1, hy1, hx2, hy2 = map(int, h.xyxy[0])
            hbox = (hx1, hy1, hx2, hy2)

            hand_near_nets = False
            hand_near_payment = False

            # --- Check NETS machine proximity ---
            for n in results.boxes:
                cls_n = int(n.cls[0])
                conf_n = float(n.conf[0])

                if cls_n == nets_id and conf_n >= PAYMENT_DETECTION_CONF_THRESHOLD:
                    nx1, ny1, nx2, ny2 = map(int, n.xyxy[0])
                    nbox = (nx1, ny1, nx2, ny2)

                    if is_touching(hbox, nbox, threshold=60):
                        hand_near_nets = True
                        break

            if not hand_near_nets:
                continue

            # --- Check card OR phone proximity ---
            for p in results.boxes:
                cls_p = int(p.cls[0])
                conf_p = float(p.conf[0])

                if cls_p in (card_id, phone_id) and conf_p >= PAYMENT_DETECTION_CONF_THRESHOLD:
                    px1, py1, px2, py2 = map(int, p.xyxy[0])
                    pbox = (px1, py1, px2, py2)

                    if is_touching(hbox, pbox, threshold=30):
                        hand_near_payment = True
                        break

            # ✅ VALID PAYMENT CONDITION
            if hand_near_nets and hand_near_payment:
                payment_valid = True
                break


    # ========== 2-SECOND HOLD CONFIRMATION ==========
    global payment_hold_start_time, payment_hold_armed

    if payment_valid:  
        # Start hold timer if this is the first frame payment becomes valid
        if payment_hold_start_time is None:
            payment_hold_start_time = current_time
            payment_hold_armed = True  # allow trigger once after hold time
            # (optional) print when starting hold
            # print("[PAYMENT] Hold started...")

        held_for = current_time - payment_hold_start_time

        # Only fire after continuously valid for PAYMENT_HOLD_SECONDS
        if held_for >= PAYMENT_HOLD_SECONDS:
            # Still respect cooldown, and only fire once per continuous hold
            if payment_hold_armed and (current_time - last_payment_reset_time > PAYMENT_RESET_COOLDOWN):
                print(f"[PAYMENT] Confirmed after {PAYMENT_HOLD_SECONDS:.1f}s hold: hand near nets + card/phone")

                if mqtt_connected and mqtt_client:
                    try:
                        if incident_active:
                            print("[PAYMENT] Blocked due to mismatch")
                        else:
                            mqtt_client.publish(MQTT_PAYMENT_RESET_TOPIC, "PAYMENT_RESET", qos=1)
                            print("[MQTT] Published payment reset")

                        last_payment_reset_time = current_time
                        payment_hold_armed = False   # don't spam while still holding
                    except Exception as e:
                        print(f"[MQTT] Payment reset error: {e}")
    else:
        # Condition broke -> reset timer so it must hold again for full 2 seconds
        payment_hold_start_time = None
        payment_hold_armed = False

    # ========== HAND + OBJECT COUNTING (MULTI-OBJECT) WITH HOLD ==========
    global holding_start_time, holding_confirmed

    # Count how many UNIQUE objects are being held (touching ANY hand)
    held_object_indices = set()

    # Pre-extract boxes for speed
    hand_list = []
    obj_list  = []   # (idx, box)
    card_list = []   # (idx, box, conf)

    for idx, b in enumerate(results.boxes):
        cls = int(b.cls[0])
        conf = float(b.conf[0])
        bx1, by1, bx2, by2 = map(int, b.xyxy[0])
        box_xyxy = (bx1, by1, bx2, by2)

        if cls == hand_id and conf >= HAND_OBJECT_CONF_THRESHOLD:
            hand_list.append(box_xyxy)

        elif cls == object_id and conf >= HAND_OBJECT_CONF_THRESHOLD:
            obj_list.append((idx, box_xyxy))

        elif cls == card_id and conf >= HAND_OBJECT_CONF_THRESHOLD:
            # we'll optionally treat some "card" as object later
            card_list.append((idx, box_xyxy, conf))

    # 1) Normal objects: object touching any hand
    for (oidx, obox) in obj_list:
        for hbox in hand_list:
            if is_touching(hbox, obox):
                held_object_indices.add(oidx)
                # GUI visual: yellow union box
                hx1, hy1, hx2, hy2 = hbox
                ox1, oy1, ox2, oy2 = obox
                cv2.rectangle(
                    region_gui,
                    (min(hx1, ox1), min(hy1, oy1)),
                    (max(hx2, ox2), max(hy2, oy2)),
                    (0, 255, 255),
                    2
                )
                break

    # # 2) Optional: if "card" is likely a misclassified object, count it too
    # # (we'll define "likely misclassified" by: NOT near payment and NOT card-like shape)
    # def is_card_like(box_xyxy):
    #     x1, y1, x2, y2 = box_xyxy
    #     w = max(1, x2 - x1)
    #     h = max(1, y2 - y1)
    #     ar = w / h
    #     # typical card aspect ratio ~ 1.5–1.7 (landscape). Allow some range.
    #     return 1.3 <= ar <= 2.0

    # for (cidx, cbox, cconf) in card_list:
    #     # If it looks NOT like a card, and it's being held -> treat as object for counting
    #     if (not is_card_like(cbox)):
    #         for hbox in hand_list:
    #             if is_touching(hbox, cbox):
    #                 held_object_indices.add(("card_as_obj", cidx))
    #                 # GUI highlight
    #                 hx1, hy1, hx2, hy2 = hbox
    #                 cx1, cy1, cx2, cy2 = cbox
    #                 cv2.rectangle(
    #                     region_gui,
    #                     (min(hx1, cx1), min(hy1, cy1)),
    #                     (max(hx2, cx2), max(hy2, cy2)),
    #                     (0, 255, 255),
    #                     2
    #                 )
    #                 break

    count_raw = len(held_object_indices)

    # -------- HOLD CONFIRMATION (count must stay stable) --------
    current_time = time.time()

    # Track stability of the COUNT (not just boolean)
    if not hasattr(run_detection, "stable_count"):
        run_detection.stable_count = 0
        run_detection.stable_since = None

    if count_raw > 0:
        if run_detection.stable_since is None or count_raw != run_detection.stable_count:
            run_detection.stable_count = count_raw
            run_detection.stable_since = current_time
            holding_confirmed = False
        else:
            if (current_time - run_detection.stable_since) >= HOLDING_CONFIRM_SECONDS:
                holding_confirmed = True
    else:
        run_detection.stable_count = 0
        run_detection.stable_since = None
        holding_confirmed = False

    count = run_detection.stable_count if holding_confirmed else 0


    # Publish ONLY when count changes
    if mqtt_connected and mqtt_client:
        if count != last_published_count:
            message = {
                "timestamp": time.time(),
                "Objects Detected": count
            }
            try:
                result = mqtt_client.publish(MQTT_PUBLISH_TOPIC, json.dumps(message), qos=1)
                print(f"[MQTT] Publish -> {message}, rc={result.rc}")
                last_published_count = count
            except Exception as e:
                print(f"[MQTT] Publish error: {e}")

        # Return:
        #   - frame (GUI annotations)
        #   - count
        #   - save_frame (only blackout, no boxes/labels)
        last_detection_result = (frame.copy(), count, save_frame.copy())
        return last_detection_result


# ===================== TKINTER GUI & ROI HANDLING =====================

def toggle_roi_display():
    if not show_roi.get() and detection_started:
        status_label.config(text="ROI display turned OFF. Detection continues in ROI.")
    elif show_roi.get() and detection_started:
        status_label.config(text="ROI display turned ON. Detection continues in ROI.")
    elif not roi_set:
        status_label.config(text="Click 'Select ROI Region' to start.")
    else:
        status_label.config(text="ROI selected. Click 'Start Detection' to begin detection in ROI.")

def on_mouse_press(event):
    global ix, iy, drawing, frame_copy, original_frame_copy, display_frame
    drawing = True
    ix = max(0, min(event.x, video_width - 1))
    iy = max(0, min(event.y, video_height - 1))
    original_frame_copy = display_frame.copy() if display_frame is not None else np.zeros((video_height, video_width, 3), dtype=np.uint8)
    frame_copy = original_frame_copy.copy()

def on_mouse_drag(event):
    global fx, fy, frame_copy
    if drawing and original_frame_copy is not None:
        fx = max(0, min(event.x, video_width - 1))
        fy = max(0, min(event.y, video_height - 1))
        frame_copy = original_frame_copy.copy()
        cv2.rectangle(frame_copy, (ix, iy), (fx, fy), (0, 255, 0), 2)
        x1, y1 = min(ix, fx), min(iy, fy)
        x2, y2 = max(ix, fx), max(iy, fy)
        w = x2 - x1
        h = y2 - y1
        roi_coords_label.config(text=f"ROI Coordinates: x: {x1}, y: {y1}, w: {w}, h: {h}")

def on_mouse_release(event):
    global drawing, fx, fy, roi, roi_set
    if drawing:
        drawing = False
        fx = max(0, min(event.x, video_width - 1))
        fy = max(0, min(event.y, video_height - 1))
        x1, y1 = min(ix, fx), min(iy, fy)
        x2, y2 = max(ix, fx), max(iy, fy)
        roi = (x1, y1, x2, y2)
        roi_set = True
        w = x2 - x1
        h = y2 - y1
        roi_coords_label.config(text=f"ROI Coordinates: x: {x1}, y: {y1}, w: {w}, h: {h}")
        video_label.unbind("<Button-1>")
        video_label.unbind("<B1-Motion>")
        video_label.unbind("<ButtonRelease-1>")
        save_roi_to_json(x1, y1, x2, y2)
        status_label.config(text="ROI selected. Click 'Start Detection' to begin detection in ROI.")

def select_roi():
    global drawing, roi_set, frame_copy, original_frame_copy, detection_started
    drawing = False
    roi_set = False
    detection_started = False
    frame_copy = None
    original_frame_copy = None
    show_roi.set(True)
    status_label.config(text="Click and drag on video to select ROI.")
    video_label.bind("<Button-1>", on_mouse_press)
    video_label.bind("<B1-Motion>", on_mouse_drag)
    video_label.bind("<ButtonRelease-1>", on_mouse_release)
    roi_coords_label.config(text="ROI Coordinates: x: 0, y: 0, w: 0, h: 0")

def start_detection():
    global detection_started
    if roi_set and not detection_started:
        detection_started = True
        status_label.config(text="Detection started within ROI.")
    elif not roi_set and not detection_started:
        detection_started = True
        status_label.config(text="Detection started in full-frame (no ROI).")
    else:
        status_label.config(text="Detection already started.")

def stop_detection():
    global roi_set, drawing, roi, frame_copy, original_frame_copy, detection_started, last_published_count
    roi_set = False
    drawing = False
    roi = None
    frame_copy = None
    original_frame_copy = None
    detection_started = False
    last_published_count = -1
    show_roi.set(True)
    status_label.config(text="Detection stopped. Click 'Select ROI Region' to start again.")
    video_label.unbind("<Button-1>")
    video_label.unbind("<B1-Motion>")
    video_label.unbind("<ButtonRelease-1>")
    roi_coords_label.config(text="ROI Coordinates: x: 0, y: 0, w: 0, h: 0")

def create_button_image(text, bg_color):
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        font = ImageFont.truetype(font_path, 20)
    except IOError:
        font = ImageFont.load_default()
    img = Image.new("RGBA", (180, 40), bg_color)
    draw = ImageDraw.Draw(img)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = (180 - text_width) // 2
    text_y = (40 - text_height) // 2
    draw.text((text_x, text_y), text, font=font, fill="black")
    return ImageTk.PhotoImage(img)

def update_video():
    global running, frame_copy, display_frame, roi, saved_gui_frame

    if not running:
        return

    with frame_lock:
        frame_full = None if latest_dome_frame is None else latest_dome_frame.copy()

    if frame_full is None:
        debug_label.config(text="Debug: No frame from RTSP stream")
        root.after(33, update_video)
        return

    # Resize to GUI video size
    # frame_resized = cv2.resize(frame, (video_width, video_height))
    # display_frame = frame_resized.copy()

    full_h, full_w = frame_full.shape[:2]

    # GUI frame
    frame_gui = cv2.resize(frame_full, (video_width, video_height))
    display_frame = frame_gui.copy()

    try:
        if drawing and frame_copy is not None:
            # User is drawing ROI - just show that frame
            final_display_frame = frame_copy.copy()
            frame_for_save = final_display_frame.copy()
            num_detections = 0
            debug_label.config(text="Debug: Drawing ROI, detection paused")
        else:
            final_display_frame = display_frame.copy()
            frame_for_save = final_display_frame.copy()
            num_detections = 0

            # Full screen ROI
            x1, y1 = 0, 0
            x2, y2 = video_width, video_height

            if detection_started:
                if roi_set and roi is not None:
                    x1 = max(0, min(roi[0], video_width - 1))
                    y1 = max(0, min(roi[1], video_height - 1))
                    x2 = max(x1 + 1, min(roi[2], video_width))
                    y2 = max(y1 + 1, min(roi[3], video_height))

                    # Get both GUI frame and "clean" save frame
                    # final_display_frame, num_detections, save_candidate = run_detection(
                    #     final_display_frame, x1, y1, x2, y2
                    # )

                    # Scale factors GUI → full-res
                    sx = full_w / video_width
                    sy = full_h / video_height

                    # Convert ROI coords
                    fx1 = int(x1 * sx)
                    fy1 = int(y1 * sy)
                    fx2 = int(x2 * sx)
                    fy2 = int(y2 * sy)

                    # Run YOLO on FULL RES frame
                    det_full, num_detections, save_candidate = run_detection(
                        frame_full.copy(), fx1, fy1, fx2, fy2
                    )

                    # Resize result back to GUI
                    final_display_frame = cv2.resize(det_full, (video_width, video_height))

                    # Save version AFTER blackout, BEFORE green ROI box
                    frame_for_save = save_candidate.copy()

                    # Draw ROI rectangle only on GUI (not on save frame)
                    if show_roi.get():
                        cv2.rectangle(final_display_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

                    debug_label.config(
                        text=f"Debug: ROI shape: {(y2 - y1, x2 - x1)}, Count: {num_detections}"
                    )
                else:
                    # Full-frame detection (no ROI)
                    # final_display_frame, num_detections, save_candidate = run_detection(
                    #     final_display_frame, 0, 0, video_width, video_height
                    # )

                    det_full, num_detections, save_candidate = run_detection(
                        frame_full.copy(), 0, 0, full_w, full_h
                    )
                    final_display_frame = cv2.resize(det_full, (video_width, video_height))

                    frame_for_save = save_candidate.copy()
                    debug_label.config(
                        text=f"Debug: Full frame mode, Count: {num_detections}"
                    )
            else:
                debug_label.config(text="Debug: Detection off or not started")

        # Slight darkening (optional, to reduce glare)
        final_display_frame = cv2.convertScaleAbs(final_display_frame, beta=-5)
        frame_for_save = cv2.convertScaleAbs(frame_for_save, beta=-5)

        # Store the "clean" version (only blackout, no ROI, no labels) for saving
        with saved_frame_lock:
            saved_gui_frame = frame_for_save.copy()

        # Convert to RGB for Tkinter display
        final_display_frame_rgb = cv2.cvtColor(final_display_frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(final_display_frame_rgb)
        photo = ImageTk.PhotoImage(image)
        video_label.config(image=photo)
        video_label.image = photo

    except Exception as e:
        debug_label.config(text=f"Debug: Error {str(e)}")

    root.after(33, update_video)

def close_program():
    global running, mqtt_client
    running = False
    try:
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            print("MQTT client cleanup successful")
    except Exception as e:
        print(f"Error during MQTT cleanup: {e}")
    try:
        root.destroy()
    except Exception:
        pass

# ===================== MAIN GUI SETUP =====================

root = tk.Tk()
root.title("YOLO RTSP Stream Detection with ROI and MQTT")

show_roi = tk.BooleanVar(value=True)

screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()
base_width = int(screen_width * 0.9)
base_height = int(screen_height * 0.7)
scale_factor = 0.9 * 1.05
target_aspect_ratio = 1920 / 1080

if (base_width / base_height) > target_aspect_ratio:
    video_height = int(base_height * scale_factor)
    video_width = int(video_height * target_aspect_ratio)
else:
    video_width = int(base_width * scale_factor)
    video_height = int(video_width / target_aspect_ratio)

video_width = max(32, (video_width // 32) * 32)
video_height = max(32, (video_height // 32) * 32)

loaded_roi = load_roi_from_json()
if loaded_roi:
    roi = loaded_roi
    roi_set = True
    detection_started = True
    x1, y1, x2, y2 = roi
    w = x2 - x1
    h = y2 - y1
    print(f"Loaded ROI from JSON: {roi}, starting detection and MQTT immediately.")
    initial_roi_label_text = f"ROI Coordinates: x: {x1}, y: {y1}, w: {w}, h: {h}"
    initial_status_text = "Detection started within saved ROI."
else:
    roi = None
    roi_set = False
    detection_started = False
    print("No saved ROI found. Waiting for user to select.")
    initial_roi_label_text = "ROI Coordinates: x: 0, y: 0, w: 0, h: 0"
    initial_status_text = "Click 'Select ROI Region' to start."

warm_up_model()

# Start RTSP (or webcam) thread
def start_camera_thread():
    if wait_for_camera(RTSP_URL):
        t = threading.Thread(target=dome_camera_loop, daemon=True)
        t.start()

# Start camera watcher thread (non-blocking)
threading.Thread(target=start_camera_thread, daemon=True).start()

# Start MQTT thread
mqtt_thread = threading.Thread(target=start_mqtt_client, daemon=True)
mqtt_thread.start()

# GUI widgets
video_label = tk.Label(root)
video_label.pack(pady=10)

button_frame = tk.Frame(root)
button_frame.pack(pady=5)

btn_select_roi_img = create_button_image("Select ROI Region", "#7f7fff")
btn_select_roi = tk.Button(button_frame, image=btn_select_roi_img, command=select_roi)
btn_select_roi.grid(row=0, column=0, padx=5)

btn_start_detection_img = create_button_image("Start Detection", "#7fff7f")
btn_start_detection = tk.Button(button_frame, image=btn_start_detection_img, command=start_detection)
btn_start_detection.grid(row=0, column=1, padx=5)

btn_stop_detection_img = create_button_image("Stop Detection", "#ff7f7f")
btn_stop_detection = tk.Button(button_frame, image=btn_stop_detection_img, command=stop_detection)
btn_stop_detection.grid(row=0, column=2, padx=5)

roi_coords_label = tk.Label(root, text=initial_roi_label_text)
roi_coords_label.pack()

status_label = tk.Label(root, text=initial_status_text, fg="blue")
status_label.pack()

debug_label = tk.Label(root, text="Debug: Waiting for frame...")
debug_label.pack()

show_roi_checkbox = tk.Checkbutton(root, text="Show ROI Rectangle", variable=show_roi, command=toggle_roi_display)
show_roi_checkbox.pack(pady=5)

root.protocol("WM_DELETE_WINDOW", close_program)

update_video()
root.mainloop()