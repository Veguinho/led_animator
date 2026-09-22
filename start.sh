#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_DIR="$PROJECT_DIR/cs2_16x16_player_firmware"
DEFAULT_FQBN="esp32:esp32:esp32s3:CDCOnBoot=default,UploadSpeed=115200"

port="auto"
fqbn="${ARDUINO_FQBN:-$DEFAULT_FQBN}"
upload=true
visualizer_args=(--style spectrum)

usage() {
  cat <<'EOF'
Usage: ./start.sh [launcher options] [visualizer options]

Compile and upload the ESP32-S3 sketch, then start the live audio spectrum.

Launcher options:
  --port PORT       USB serial port (default: auto-detect)
  --fqbn FQBN       Arduino board identifier (default: ESP32-S3, CH340/UART, 115200 upload)
  --no-upload       Skip compilation/upload and only restart streaming
  -h, --help        Show this help

All other options are passed to system_audio_visualizer.py. Examples:
  ./start.sh
  ./start.sh --no-upload --sensitivity 1.5
  ./start.sh --port /dev/cu.usbserial-1420 --style wave
EOF
}

while (($#)); do
  case "$1" in
    --port)
      [[ $# -ge 2 ]] || { echo "error: --port needs a value" >&2; exit 2; }
      port="$2"
      shift 2
      ;;
    --port=*)
      port="${1#*=}"
      shift
      ;;
    --fqbn)
      [[ $# -ge 2 ]] || { echo "error: --fqbn needs a value" >&2; exit 2; }
      fqbn="$2"
      shift 2
      ;;
    --fqbn=*)
      fqbn="${1#*=}"
      shift
      ;;
    --no-upload)
      upload=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      visualizer_args+=("$@")
      break
      ;;
    *)
      visualizer_args+=("$1")
      shift
      ;;
  esac
done

if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  project_python="$PROJECT_DIR/.venv/bin/python"
else
  project_python="$(command -v python3 || true)"
fi

if [[ -z "$project_python" ]]; then
  echo "error: python3 was not found" >&2
  exit 1
fi

if [[ "$port" == "auto" ]]; then
  echo "Detecting the USB controller..." >&2
  port="$($project_python -c \
    'from stream_arduino import resolve_port; print(resolve_port("auto", wait_timeout=30))')"
fi

if [[ "$upload" == true ]]; then
  arduino_cli="${ARDUINO_CLI:-$(command -v arduino-cli || true)}"
  if [[ -z "$arduino_cli" && -x "$PROJECT_DIR/.build/tools/arduino-cli" ]]; then
    arduino_cli="$PROJECT_DIR/.build/tools/arduino-cli"
  fi
  if [[ -z "$arduino_cli" || ! -x "$arduino_cli" ]]; then
    echo "error: arduino-cli is required to upload the sketch" >&2
    echo "Install it with: brew install arduino-cli" >&2
    echo "Then rerun ./start.sh, or use ./start.sh --no-upload now." >&2
    exit 1
  fi

  echo "Compiling the ESP32 sketch for $fqbn..." >&2
  "$arduino_cli" compile --fqbn "$fqbn" --build-path "$PROJECT_DIR/.build/panel48" "$SKETCH_DIR"
  "$project_python" -c 'from device_modes import stop_mode_workers; stop_mode_workers()'
  echo "Uploading the sketch through $port..." >&2
  "$arduino_cli" upload --port "$port" --fqbn "$fqbn" --input-dir "$PROJECT_DIR/.build/panel48" "$SKETCH_DIR"
fi

"$project_python" -c 'from device_modes import stop_mode_workers; stop_mode_workers()'
echo "Starting the live spectrum on $port..." >&2
exec "$project_python" "$PROJECT_DIR/system_audio_visualizer.py" \
  --display-size 48 \
  --fps 12 \
  --port "$port" \
  --clear-on-exit \
  "${visualizer_args[@]}"
