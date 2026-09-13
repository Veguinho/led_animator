"""Board operations shared by the two mode launchers."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parent
AUDIO_FQBN = "esp32:esp32:esp32s3:CDCOnBoot=default,UploadSpeed=115200"
VIDEO_FQBN = AUDIO_FQBN + ",PSRAM=opi"


def stop_mode_workers() -> None:
    """Gracefully stop only audio/video workers belonging to this checkout."""
    from audio_service import stop
    stop()
    targets = {str(ROOT / "system_audio_visualizer.py"), str(ROOT / "preloaded_video/player.py")}
    rows = subprocess.check_output(["ps", "ax", "-o", "pid=,command="], text=True)
    for row in rows.splitlines():
        fields = row.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            args = shlex.split(fields[1])
        except ValueError:
            continue
        if len(args) < 2 or args[1] not in targets or "python" not in Path(args[0]).name.lower():
            continue
        pid = int(fields[0])
        if pid == os.getpid() or "--prepare-only" in args:
            continue
        try:
            os.kill(pid, signal.SIGINT)  # Runs the worker's audio/USB cleanup.
        except ProcessLookupError:
            continue
        print(f"Stopping the previous mode (PID {pid})...")
        for _ in range(50):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            raise RuntimeError("previous mode did not exit; stop it before switching modes")


def flash_firmware(mode: str, port: str) -> None:
    cli = os.environ.get("ARDUINO_CLI") or shutil.which("arduino-cli")
    cli = cli or str(ROOT / ".build/tools/arduino-cli")
    if not Path(cli).is_file():
        raise RuntimeError("arduino-cli is required to switch firmware")
    if mode == "audio":
        sketch = ROOT / "cs2_16x16_player_firmware"
        fqbn = AUDIO_FQBN
        build = ROOT / ".build/panel32"
    else:
        sketch = ROOT / "preloaded_video/preloaded_video_firmware"
        fqbn = VIDEO_FQBN
        build = ROOT / ".build/preloaded-video"
    print(f"Compiling {mode} firmware...", flush=True)
    subprocess.run([cli, "compile", "--fqbn", fqbn, "--build-path", str(build), str(sketch)], check=True)
    stop_mode_workers()
    print(f"Uploading {mode} firmware through {port}...", flush=True)
    subprocess.run([cli, "upload", "--port", port, "--fqbn", fqbn,
                    "--input-dir", str(build), str(sketch)], check=True)
