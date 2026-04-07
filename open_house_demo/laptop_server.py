#!/usr/bin/env python3
# YOLOv8l inference server — MacBook side of the split compute pipeline
import threading
import time

MODEL = "yolov8l.pt"
HOST = "0.0.0.0"
PORT = 5051

import numpy as np

try:
    import cv2
except ImportError:
    raise SystemExit("opencv-python not installed — run: pip install opencv-python")

try:
    from ultralytics import YOLO
except ImportError:
    raise SystemExit("ultralytics not installed — run: pip install ultralytics")

from flask import Flask, request, jsonify

try:
    import torch
    if torch.backends.mps.is_available():
        DEVICE = "mps"
    elif torch.cuda.is_available():
        DEVICE = "cuda"
    else:
        DEVICE = "cpu"
except Exception:
    DEVICE = "cpu"

print(f"[Server] Device: {DEVICE}")
print(f"[Server] Loading {MODEL} ...")
_model = YOLO(MODEL)
_model_lock = threading.Lock()
print(f"[Server] Model ready — accepting frames on :{PORT}")

_stats = {
    "total_requests": 0,
    "last_inference_ms": 0.0,
    "started_at": time.time(),
}

app = Flask(__name__)


@app.route("/ping")
def ping():
    uptime = round(time.time() - _stats["started_at"])
    return jsonify(
        ok=True,

        model=MODEL,
        device=DEVICE,
        total_requests=_stats["total_requests"],
        last_inference_ms=_stats["last_inference_ms"],
        uptime_s=uptime,
    )


@app.route("/detect", methods=["POST"])
def detect():
    raw = request.data
    if not raw:
        return jsonify(error="No image data"), 400

    conf = float(request.args.get("conf", 0.30))
    accepted_raw = request.args.get("accepted", "sports ball")
    accepted = {a.strip().lower() for a in accepted_raw.split(",") if a.strip()}

    arr = np.frombuffer(raw, np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify(error="Could not decode image"), 400

    t0 = time.time()
    with _model_lock:
        results = _model(frame, verbose=False, conf=conf, device=DEVICE)
    elapsed_ms = (time.time() - t0) * 1000

    _stats["total_requests"] += 1
    _stats["last_inference_ms"] = round(elapsed_ms, 1)

    detections = []
    for result in results:
        for box in result.boxes:
            cls_id = int(box.cls[0])
            label = _model.names[cls_id]
            c = float(box.conf[0])
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            bw = int(x2 - x1)
            bh = int(y2 - y1)
            detections.append({
                "label": label,
                "conf": round(c, 3),
                "cx": cx,
                "cy": cy,
                "x1": int(x1),
                "y1": int(y1),
                "x2": int(x2),
                "y2": int(y2),
                "w": bw,
                "h": bh,
                "is_target": label.lower() in accepted,
            })

    return jsonify(
        detections=detections,
        inference_ms=round(elapsed_ms, 1),
        model=MODEL,
    )


if __name__ == "__main__":
    print(f"[Server] http://{HOST}:{PORT}")
    print(f"[Server]   GET  /ping")
    print(f"[Server]   POST /detect  (raw JPEG body)")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
