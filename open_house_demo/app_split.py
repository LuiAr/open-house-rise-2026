#!/usr/bin/env python3
# streams frames to laptop_server.py for YOLO inference, runs the dashboard and robot control
import cv2
import time
import threading
import platform
import os
import re
from collections import deque

from flask import Flask, Response, request, jsonify, render_template

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("[Warning] requests not installed — pip install requests")


# Pi detection
def is_raspberry_pi():
    try:
        with open("/proc/device-tree/model") as f:
            return "raspberry pi" in f.read().lower()
    except Exception:
        return False

IS_PI = is_raspberry_pi()
print(f"[Platform] Running on {'Raspberry Pi' if IS_PI else 'non-Pi (robot commands will print only)'}")

# ROS 2 robot control (only initialised on Pi)
HAS_ROS = False
_ros_node = None
_drive_pub = None

if IS_PI:
    try:
        import rclpy
        from rclpy.node import Node as _RosNode
        from hqv_public_interface.msg import RemoteDriverDriveCommand as _DriveCmd

        rclpy.init()

        class _BallFollower(_RosNode):
            def __init__(self):
                super().__init__("ball_follower_split")

        _ros_node = _BallFollower()
        _drive_pub = _ros_node.create_publisher(
            _DriveCmd, "/hqv_mower/remote_driver/drive", 10
        )
        import threading as _threading
        _ros_spin_thread = _threading.Thread(target=rclpy.spin, args=(_ros_node,), daemon=True)
        _ros_spin_thread.start()
        HAS_ROS = True
        print("[ROS2] Publisher ready on /hqv_mower/remote_driver/drive")
        print("[ROS2] Spin thread started")
    except Exception as _e:
        print(f"[ROS2] Not available: {_e}")

# Camera auto-detect
def find_camera(max_index=10):
    if platform.system() == "Linux":
        base = "/sys/class/video4linux"
        if os.path.isdir(base):
            for entry in sorted(os.listdir(base)):
                name_path = os.path.join(base, entry, "name")
                if not os.path.isfile(name_path):
                    continue
                with open(name_path) as f:
                    name = f.read().strip()
                if "obsbot" in name.lower() or "meet" in name.lower():
                    m = re.search(r"(\d+)$", entry)
                    if m:
                        idx = int(m.group(1))
                        print(f"[Camera] Found '{name}' at index {idx}")
                        return idx
    if platform.system() == "Darwin":
        # Try to find OBSBOT via system_profiler camera list (order matches OpenCV indices)
        try:
            import subprocess
            result = subprocess.run(
                ["system_profiler", "SPCameraDataType"],
                capture_output=True, text=True, timeout=4
            )
            cameras = [l.strip() for l in result.stdout.splitlines() if l.strip().endswith(":") and l.strip() != "Cameras:"]
            for i, cam_name in enumerate(cameras):
                if "obsbot" in cam_name.lower() or "meet" in cam_name.lower():
                    print(f"[Camera] macOS — found '{cam_name.rstrip(':')}' at index {i}")
                    return i
        except Exception:
            pass
        # Fallback: if more than one camera is connected, skip index 0 (built-in)
        cap1 = cv2.VideoCapture(1)
        if cap1.isOpened():
            cap1.release()
            print("[Camera] macOS — using index 1 (built-in is 0, external likely 1)")
            return 1
        print("[Camera] macOS — defaulting to index 0")
        return 0
    print("[Camera] Defaulting to index 0")
    return 0

# Runtime settings
settings = {
    # CHANGE HERE FOR YOUR LAPTOP
    "laptop_url": "http://Luis-MacBook-Pro.local:5051",
    "yolo_conf": 0.40,
    "yolo_show_all": False,
    "yolo_accepted": ["sports ball","orange"],
    # Robot movement
    "robot_enabled": False,
    "forward_speed": 0.30,
    "turn_steering": 1.5,
    "pulse_duration": 0.25,
    "eval_interval": 0.40,
    # Scared mode
    "scared_enabled": False,
    "scary_objects": ["cell phone"],
    "scared_speed": 0.20,
}

# Remote server connection state
remote_status = {
    "connected": False,
    "latency_ms": 0.0,
    "inference_ms": 0.0,
    "error": None,
    "model": None,
}
remote_lock = threading.Lock()

# Robot state
robot_last_detected_time = 0.0
robot_last_command = "STOP"
robot_lock = threading.Lock()


# Remote detection (sends frame to MacBook server)
def detect_remote(frame):
    if not HAS_REQUESTS:
        return None, [], None

    url = settings["laptop_url"].rstrip("/") + "/detect"
    conf = settings["yolo_conf"]
    accepted = ",".join(settings["yolo_accepted"])

    ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ret:
        return None, [], None

    t_start = time.time()
    try:
        resp = _requests.post(
            url,
            data=buf.tobytes(),
            params={"conf": conf, "accepted": accepted},
            timeout=1.0,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        with remote_lock:
            remote_status["connected"] = False
            remote_status["error"] = str(e)
        return None, [], None

    round_trip_ms = (time.time() - t_start) * 1000
    with remote_lock:
        remote_status["connected"] = True
        remote_status["latency_ms"] = round(round_trip_ms, 1)
        remote_status["inference_ms"] = data.get("inference_ms", 0.0)
        remote_status["model"] = data.get("model")
        remote_status["error"] = None

    all_dets = data.get("detections", [])
    targets = [d for d in all_dets if d["is_target"]]
    best = None
    if targets:
        best = max(targets, key=lambda d: d["w"] * d["h"])
        best["radius"] = max(best["w"], best["h"]) // 2
        best["area"] = best["w"] * best["h"]
    return best, all_dets, None

def detect(frame):
    return detect_remote(frame)


# Annotation
def annotate(frame, target, all_dets, fps, is_scared=False):
    fh, fw = frame.shape[:2]

    if settings["yolo_show_all"]:
        for d in all_dets:
            if d.get("is_target"):
                continue
            if "x1" in d:
                cv2.rectangle(frame, (d["x1"], d["y1"]), (d["x2"], d["y2"]), (180, 180, 180), 1)
                cv2.putText(frame, f"{d['label']} {d['conf']:.2f}",
                            (d["x1"], d["y1"] - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)

    zone_left = int(fw * 0.25)
    zone_right = int(fw * 0.75)
    cv2.line(frame, (zone_left, 0), (zone_left, fh), (0, 150, 200), 1)
    cv2.line(frame, (zone_right, 0), (zone_right, fh), (0, 150, 200), 1)

    trail = list(ball_trail)
    n = len(trail)
    if n > 1:
        for i, (tx, ty) in enumerate(trail):
            age = (i + 1) / n
            radius = max(1, round(3 * age))
            brightness = int(80 + 100 * age)
            cv2.circle(frame, (tx, ty), radius, (brightness, brightness + 20, 80), -1)

    if target:
        cx_px = target["cx"]
        if cx_px < zone_left:
            zone = "LEFT"
            zone_colour = (0, 165, 255)
        elif cx_px > zone_right:
            zone = "RIGHT"
            zone_colour = (0, 165, 255)
        else:
            zone = "FORWARD"
            zone_colour = (0, 220, 160)

        target["zone"] = zone

        if "x1" in target:
            cv2.rectangle(frame, (target["x1"], target["y1"]),
                          (target["x2"], target["y2"]), zone_colour, 2)
        elif "radius" in target:
            cv2.circle(frame, (cx_px, target["cy"]), target["radius"], zone_colour, 2)
        cv2.circle(frame, (cx_px, target["cy"]), 5, (0, 0, 255), -1)
        cv2.putText(frame,
                    f"{target['label']}  {zone}  conf={target['conf']:.2f}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.65, zone_colour, 2)
    else:
        cv2.putText(frame, "No ball detected", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    label = f"[REMOTE] FPS: {fps:.1f}"
    cv2.putText(frame, label, (fw - 240, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    if is_scared and settings["scared_enabled"]:
        cv2.rectangle(frame, (0, 0), (fw, fh), (0, 0, 220), 5)
        cv2.putText(frame, "SCARED!", (fw // 2 - 80, fh // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 220), 4)
    return frame


# Camera state
latest_frame = None
latest_mask = None
latest_detection = None
latest_all_dets = []
latest_scared = False
actual_fps = 0.0
frame_lock = threading.Lock()
ball_trail = deque(maxlen=28)


def _open_camera(cam_index, width, height):
    cap = cv2.VideoCapture(cam_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not cap.isOpened():
        cap.release()
        return None
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[Camera] Opened index {cam_index}: {actual_w}x{actual_h}")
    return cap


def capture_loop(cam_index, width, height):
    global latest_frame, latest_mask, latest_detection, latest_all_dets, latest_scared, actual_fps

    cap = None
    while cap is None:
        print(f"[Camera] Attempting to open index {cam_index} ...")
        cap = _open_camera(cam_index, width, height)
        if cap is None:
            print("[Camera] Could not open — retrying in 3 s ...")
            time.sleep(3)

    consecutive_failures = 0

    while True:
        t0 = time.time()
        ret, frame = cap.read()
        if not ret:
            consecutive_failures += 1
            if consecutive_failures >= 10:
                print("[Camera] Too many read failures — attempting reconnect ...")
                cap.release()
                time.sleep(2)
                cap = _open_camera(cam_index, width, height)
                if cap is None:
                    print("[Camera] Reconnect failed - will retry ...")
                    time.sleep(3)
                    cap = _open_camera(cam_index, width, height) or cv2.VideoCapture(cam_index)
                consecutive_failures = 0
            time.sleep(0.05)
            continue
        consecutive_failures = 0
        target, all_dets, mask = detect(frame)
        scary_set = {s.lower() for s in settings["scary_objects"]}
        is_scared = bool(scary_set and any(d["label"].lower() in scary_set for d in all_dets))
        if target:
            ball_trail.append((target["cx"], target["cy"]))
        annotated = annotate(frame.copy(), target, all_dets, actual_fps, is_scared)
        with frame_lock:
            latest_frame = annotated
            latest_mask = mask
            latest_detection = target
            latest_all_dets = all_dets
            latest_scared = is_scared
        elapsed = time.time() - t0
        actual_fps = 1.0 / max(elapsed, 0.001)


# Robot drive helpers
def _publish_drive(speed, steering):
    if IS_PI and HAS_ROS:
        msg = _DriveCmd()
        msg.header.stamp = _ros_node.get_clock().now().to_msg()
        msg.speed = float(speed)
        msg.steering = float(steering)
        _drive_pub.publish(msg)
        rclpy.spin_once(_ros_node, timeout_sec=0.05)
    else:
        print(f"[Robot] speed={speed:.2f}  steering={steering:.2f}")

def _publish_stop():
    _publish_drive(0.0, 0.0)


def robot_loop():
    global robot_last_detected_time, robot_last_command
    no_ball_stop_sent = True
    while True:
        time.sleep(settings["eval_interval"])

        if not settings["robot_enabled"]:
            if not no_ball_stop_sent:
                _publish_stop()
                with robot_lock:
                    robot_last_command = "STOP"
                no_ball_stop_sent = True
            continue

        with frame_lock:
            det = latest_detection
            scared = latest_scared

        now = time.time()

        if settings["scared_enabled"] and scared:
            _publish_drive(-settings["scared_speed"], 0.0)
            with robot_lock:
                robot_last_command = "SCARED — REVERSING"
            time.sleep(settings["pulse_duration"])
            _publish_stop()
            continue

        if det:
            no_ball_stop_sent = False
            with robot_lock:
                robot_last_detected_time = now
            zone = det.get("zone", "FORWARD")

            if zone == "FORWARD":
                spd = settings["forward_speed"]
                steer = 0.0
                cmd = "FORWARD"
            elif zone == "LEFT":
                spd = 0.0
                steer = settings["turn_steering"]
                cmd = "ROTATE LEFT"
            else:
                spd = 0.0
                steer = -settings["turn_steering"]
                cmd = "ROTATE RIGHT"

            # Send drive command
            _publish_drive(spd, steer)
            with robot_lock:
                robot_last_command = cmd
            time.sleep(settings["pulse_duration"])
            _publish_stop()
        else:
            with robot_lock:
                last_seen = robot_last_detected_time
            if now - last_seen > 0.5 and not no_ball_stop_sent:
                _publish_stop()
                with robot_lock:
                    robot_last_command = "STOP"
                no_ball_stop_sent = True


# Flask app
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index_split.html")


@app.route("/logo/<name>")
def serve_logo(name):
    from flask import send_from_directory
    files = {"rise": "RISE_logo.png", "husqvarna": "husq_logo.png"}
    filename = files.get(name)
    if not filename:
        return "", 404
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)


def gen_video():
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(0.03)
            continue
        ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ret:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
        time.sleep(0.03)


@app.route("/video_feed")
def video_feed():
    return Response(gen_video(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def api_status():
    with frame_lock:
        det = latest_detection
        all_d = list(latest_all_dets)
    with remote_lock:
        r_status = dict(remote_status)
    with robot_lock:
        r_cmd = robot_last_command

    data = {
        "fps": round(actual_fps, 1),
        "mode": "remote",
        "is_pi": IS_PI,
        "robot_enabled": settings["robot_enabled"],
        "robot_command": r_cmd,
        "server": r_status,
        "scared_enabled": settings["scared_enabled"],
        "is_scared": latest_scared,
    }
    if det:
        data["detected"] = True
        data["target"] = det
        data["zone"] = det.get("zone", "UNKNOWN")
    else:
        data["detected"] = False
        data["zone"] = None
    data["all_detections"] = all_d
    return jsonify(data)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(dict(settings))


@app.route("/api/settings", methods=["POST"])
def set_settings():
    data = request.json or {}

    float_keys = ["forward_speed", "turn_steering", "pulse_duration", "eval_interval", "yolo_conf"]
    for k in float_keys:
        if k in data:
            settings[k] = float(data[k])

    if "pick_colour" in data:
        settings["pick_colour"] = data["pick_colour"]
    if "yolo_show_all" in data:
        settings["yolo_show_all"] = bool(data["yolo_show_all"])
    if "yolo_accepted" in data:
        settings["yolo_accepted"] = [s.strip().lower() for s in data["yolo_accepted"] if s.strip()]
    if "laptop_url" in data:
        settings["laptop_url"] = data["laptop_url"].strip()
        with remote_lock:
            remote_status["connected"] = False
            remote_status["error"] = None
    if "robot_enabled" in data:
        settings["robot_enabled"] = bool(data["robot_enabled"])
    if "scared_enabled" in data:
        settings["scared_enabled"] = bool(data["scared_enabled"])
    if "scary_objects" in data:
        settings["scary_objects"] = [s.strip().lower() for s in data["scary_objects"] if s.strip()]
    if "scared_speed" in data:
        settings["scared_speed"] = float(data["scared_speed"])

    return jsonify(ok=True)


@app.route("/qr_linkedin.png")
def serve_qr():
    from flask import send_from_directory
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "qr_linkedin.png")


@app.route("/api/ping_server")
def ping_server():
    """Pi pings the laptop server and returns its status."""
    if not HAS_REQUESTS:
        return jsonify(ok=False, error="requests library not installed")
    url = settings["laptop_url"].rstrip("/") + "/ping"
    try:
        resp = _requests.get(url, timeout=2.0)
        resp.raise_for_status()
        return jsonify(ok=True, **resp.json())
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# Main
if __name__ == "__main__":
    cam_index = find_camera()
    port = 5050
    width = 640
    height = 480

    t = threading.Thread(target=capture_loop, args=(cam_index, width, height), daemon=True)
    t.start()

    r = threading.Thread(target=robot_loop, daemon=True)
    r.start()

    print(f"[App] Dashboard at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
