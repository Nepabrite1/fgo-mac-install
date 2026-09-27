#!/bin/bash
# Install the FGO AI runtimes + recognition model files on the Mac (Intel).
# Runs mediapipe/openvino/onnxruntime installs, then downloads the core models
# into the machine models dir, then restarts the ai + core services.
set -e

SRC="$HOME/fgo/app-source"
MODELS="$HOME/.local/share/first-general-order-machine/models"
PD="$MODELS/person-detection-retail-0013/FP16"
mkdir -p "$MODELS" "$PD"

echo "=================================================================="
echo " Step 1/3 - Install AI runtimes (mediapipe, openvino, onnxruntime)"
echo "=================================================================="
python3 -m pip install --user mediapipe openvino onnxruntime \
  || echo "WARN: one or more pip installs had issues; continuing to models."

fetch() {
  local dest="$1"; shift
  for url in "$@"; do
    if curl -fL --retry 2 -o "$dest" "$url" 2>/dev/null && [ -s "$dest" ]; then
      echo "OK  $dest ($(wc -c < "$dest") bytes) <- $(echo "$url" | sed 's|.*/||')"
      return 0
    fi
  done
  echo "FAILED $dest (tried all URLs)"
  return 1
}

echo "=================================================================="
echo " Step 2/3 - Download recognition model files"
echo "=================================================================="
fetch "$MODELS/face_detection_yunet_2023mar.onnx" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

fetch "$MODELS/face_recognition_sface_2021dec.onnx" \
  "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"

fetch "$PD/person-detection-retail-0013.xml" \
  "https://storage.openvinotoolkit.org/repositories/open_model_zoo/temp/person-detection-retail-0013/FP16/person-detection-retail-0013.xml"

fetch "$PD/person-detection-retail-0013.bin" \
  "https://storage.openvinotoolkit.org/repositories/open_model_zoo/temp/person-detection-retail-0013/FP16/person-detection-retail-0013.bin"

# Optional but improves detection: YOLO person detector fallback + pose.
fetch "$MODELS/yolo11n.onnx" \
  "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.onnx" || echo "  (yolo optional; skipped)"

fetch "$MODELS/pose_landmarker_heavy.task" \
  "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task" \
  || echo "  (pose optional; skipped)"

echo "=================================================================="
echo " Step 3/3 - Restart ai + core services (load new runtimes/models)"
echo "=================================================================="
launchctl kickstart -k "gui/$(id -u)/com.firstgeneralorder.ai" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/com.firstgeneralorder.core" 2>/dev/null || true

echo ""
echo "DONE. Models present:"
ls -l "$MODELS"
echo ""
echo "Reopen the Live Cameras / People views and watch for recognition."
