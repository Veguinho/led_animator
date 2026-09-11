#!/usr/bin/env python3
"""Record 16 seconds of the running Adaptive preview and Mac system audio."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from system_audio_visualizer import AudioCapture, SAMPLE_RATE
from scripts.render_readme_previews import DESTINATION, FPS, SECONDS, export


class RecordingCapture(AudioCapture):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self.count = 0
        self.record_lock = threading.Lock()
        self.ready = threading.Event()

    def _append(self, samples):
        super()._append(samples)
        with self.record_lock:
            self.chunks.append(samples.copy())
            self.count += len(samples)
        self.ready.set()

    def position(self):
        with self.record_lock:
            return self.count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controls-port", type=int, default=8765)
    args = parser.parse_args()

    def state():
        with urlopen(f"http://127.0.0.1:{args.controls_port}/api/state", timeout=2) as response:
            return json.load(response)

    initial = state()
    if initial["settings"]["preset"] != "adaptive":
        parser.error("select Adaptive in the running visualizer before recording")
    capture = RecordingCapture()
    frames = []
    palettes = []
    lateness = []
    try:
        capture.start(permission_timeout=10)
        if not capture.ready.wait(timeout=3):
            raise RuntimeError("no audio samples arrived")
        first_sample = capture.position()
        start = time.monotonic()
        print(f"Recording {SECONDS}s of live Adaptive frames and system audio…", flush=True)
        for index in range(FPS * SECONDS):
            deadline = start + index / FPS
            time.sleep(max(0, deadline - time.monotonic()))
            current = state()
            lateness.append(time.monotonic() - deadline)
            if (current["session"] != initial["session"]
                    or current["settings"] != initial["settings"]):
                raise RuntimeError("the visualizer restarted or settings changed; record again")
            frames.append(np.asarray(current["frame"], dtype=np.uint8))
            palettes.append(current["palette"])
        end_sample = first_sample + SAMPLE_RATE * SECONDS
        timeout = start + SECONDS + 3
        while capture.position() < end_sample and time.monotonic() < timeout:
            time.sleep(.01)
    finally:
        capture.close()

    samples = np.concatenate(capture.chunks)[first_sample:end_sample]
    if len(samples) != SAMPLE_RATE * SECONDS:
        raise RuntimeError("audio capture ended before the recording was complete")
    if not np.all(np.isfinite(samples)) or np.max(np.abs(samples)) < 1e-5:
        raise RuntimeError("no audible system audio; play music and record again")
    if not np.any(frames):
        raise RuntimeError("the live preview stayed dark; check the running visualizer")
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    audio = output / "adaptive-system-audio.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SAMPLE_RATE),
        "-ac", "1", "-i", "pipe:0", "-c:a", "pcm_s16le", str(audio),
    ], input=samples.astype("<f4").tobytes(), check=True)
    np.savez_compressed(output / "adaptive-live-frames.npz", frames=frames, palettes=palettes)
    (output / "adaptive-settings.json").write_text(json.dumps(initial["settings"], indent=2) + "\n")
    DESTINATION.mkdir(parents=True, exist_ok=True)
    export("audio-adaptive", frames, audio)
    print(f"Maximum sampling delay: {max(lateness) * 1000:.1f} ms", flush=True)


if __name__ == "__main__":
    main()
