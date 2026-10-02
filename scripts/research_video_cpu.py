#!/usr/bin/env python3
"""Reproducible CPU search: real decoder, audio overlay, encoding and HTTP preview.

Does not acquire the serial port. Board timing/ACKs need a separate live check.
CPU is process + FFmpeg CPU seconds / wall seconds (100% = one core).
The synthetic, changing audio and fixed video seek keep trials comparable.
"""
from __future__ import annotations

import argparse
import ctypes
import http.client
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import random
import re
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def process_cpu(pid: int) -> float:
    if sys.platform == "darwin":
        # rusage_info_v2 starts with UUID[16], user/system nanoseconds.
        buffer = ctypes.create_string_buffer(256)
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        if lib.proc_pid_rusage(pid, 2, ctypes.byref(buffer)) != 0:
            raise OSError(f"Cannot read CPU counters for PID {pid}")
        counters = (ctypes.c_uint64 * 2).from_buffer(buffer, 16)
        return (counters[0] + counters[1]) / 1e9
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def poll_preview(url, config, stop, requests, error):
    """Browser surrogate in a separate process; its CPU is not backend CPU."""
    address = urlsplit(url)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
    try:
        while not stop.is_set():
            deadline = time.monotonic() + 1 / config.get("poll_fps", 60)
            path = "/api/state?compact=1" if config.get("compact") else "/api/state"
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"preview returned HTTP {response.status}")
            json.loads(response.read())
            requests.value += 1
            stop.wait(max(0, deadline - time.monotonic()))
    except Exception as exc:
        error.value = str(exc).encode()[:2047]
    finally:
        connection.close()


def thermal_limit():
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True)
    match = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", result.stdout)
    return int(match[1]) if match else None


def trial(args) -> dict:
    if args.modules:
        sys.path.insert(0, str(Path(args.modules).resolve()))
    import numpy as np
    import led_animator
    import stream_arduino
    from audio_palette_controls import PaletteControls, PaletteServer, default_settings
    from stream_arduino import VideoFrameEncoder, VideoFlashLimiter
    from video_audio_texture import LiveAudioVideoTexture
    from video_controls import VideoPlaybackControls

    config = json.loads(args.config)
    config.setdefault("compression", getattr(stream_arduino, "STREAM_COMPRESSION_LEVEL", 3))
    original_popen = subprocess.Popen
    decoders = []

    def decoder_popen(command, *positional, **keywords):
        if Path(command[0]).name == "ffmpeg":
            options = []
            if config.get("threads") is not None:
                options += ["-threads", str(config["threads"])]
            if config.get("filter_threads") is not None:
                options += ["-filter_threads", str(config["filter_threads"])]
            if config.get("hwaccel"):
                options += ["-hwaccel", config["hwaccel"]]
            command = list(command)
            if "threads" in config:
                for flag in ("-threads", "-filter_threads", "-hwaccel"):
                    while flag in command:
                        position = command.index(flag)
                        del command[position:position+2]
            command[command.index("-i"):command.index("-i")] = options
            # Override any production thread settings for the experiment.
            process = original_popen(command, *positional, **keywords)
            decoders.append(process)
            return process
        return original_popen(command, *positional, **keywords)

    subprocess.Popen = decoder_popen
    info = led_animator.probe_video(args.video)
    controls = PaletteControls(size=48)
    controls.update(default_settings() | {"preset": "adaptive", "brightness": 0.969,
                                          "saturation": 1.0, "slowdown": 0.0})
    video = VideoPlaybackControls(args.video.name, 3600, info.fps)
    samples = np.zeros(1024, dtype=np.float32)
    texture = LiveAudioVideoTexture(controls, video, lambda count: samples,
                                   size=48, immediate=False)
    encoder = VideoFrameEncoder(info.fps, 2.2, 48, VideoFlashLimiter(info.fps), texture)
    server = PaletteServer(controls, 0, video_controls=video, video_path=args.video)
    server.start()
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    requests = context.Value("i", 0)
    poll_error = context.Array("c", 2048)
    client = context.Process(target=poll_preview,
                             args=(server.url, config, stop, requests, poll_error), daemon=True)
    frames = led_animator.iter_square_video_frames(args.video, info, 48,
                                                  start_seconds=args.start)
    next(frames)  # decoder startup is excluded from steady-state counters
    client.start()
    initial_thermal_limit = thermal_limit()
    count = 0
    phase = np.arange(1024) / 48000
    cpu_start = None
    begin = time.monotonic()
    measure_start = begin + args.warmup
    measured_frames = 0
    measured_requests = 0
    profile = None
    if args.profile:
        import cProfile
        profile = cProfile.Profile()
        profile.enable()
    try:
        while time.monotonic() < measure_start + args.seconds:
            raw = next(frames)
            # Bass/mids/treble and silence all recur in each measurement.
            amplitude = 0.10 + 0.09 * np.sin(count * 0.17)
            samples[:] = amplitude * (np.sin(2*np.pi*110*phase + count*0.2)
                + 0.6*np.sin(2*np.pi*900*phase) + 0.3*np.sin(2*np.pi*6000*phase))
            if count % 60 > 50:
                samples[:] = 0
            payload = encoder.prepare(raw.tobytes())
            zlib.compress(payload, config.get("compression", 3))
            encoder.publish(payload)
            video.set_position(args.start + count / info.fps)
            count += 1
            if cpu_start is None and time.monotonic() >= measure_start:
                cpu_start = [process_cpu(os.getpid()), process_cpu(decoders[-1].pid)]
                measured_frames, measured_requests = count, requests.value
                measure_start = time.monotonic()
            time.sleep(max(0, begin + count / info.fps - time.monotonic()))
        end = time.monotonic()
        cpu_end = [process_cpu(os.getpid()), process_cpu(decoders[-1].pid)]
        elapsed = end - measure_start
        cpu = [(b-a) / elapsed * 100 for a, b in zip(cpu_start, cpu_end)]
        result = {"config": config, "cpu_pct": sum(cpu), "python_cpu_pct": cpu[0],
                  "decoder_cpu_pct": cpu[1], "fps": (count-measured_frames)/elapsed,
                  "preview_fps": (requests.value-measured_requests)/elapsed,
                  "seconds": elapsed,
                  "cpu_speed_limit_start": initial_thermal_limit,
                  "cpu_speed_limit_end": thermal_limit(),
                  "poll_errors": [poll_error.value.decode()] if poll_error.value else []}
        result["valid"] = result["fps"] >= info.fps * 0.97 and not poll_error.value
        return result
    finally:
        if profile:
            profile.disable()
            profile.dump_stats(args.profile)
        stop.set()
        client.join(timeout=3)
        if client.is_alive():
            client.terminate()
            client.join(timeout=2)
        frames.close()
        server.close()


def search(args):
    destination = args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261001)
    candidates = [dict(threads=t, filter_threads=f, hwaccel=h, compression=c,
                       poll_fps=args.poll_fps, compact=args.compact)
                  for t, f, h, c in itertools.product(
                      (1, 2, 3, 4, 6, 8, 12, 0), (1, 2, 4),
                      (None, "videotoolbox") if sys.platform == "darwin" else (None,),
                      (1, 3, 6))]
    rng.shuffle(candidates)
    # First establish the current application's unchanged settings.
    source_root = Path(args.modules) if args.modules else ROOT
    compression = re.search(r"^STREAM_COMPRESSION_LEVEL = (\d+)$",
                            (source_root/"stream_arduino.py").read_text(), re.M)
    baseline = {"poll_fps": args.poll_fps,
                "compression": int(compression[1]) if compression else 3,
                "compact": args.compact}
    best = None
    stale = 0
    def measure(config):
        command = [sys.executable, str(Path(__file__).resolve()), str(args.video),
                   "--worker", "--config", json.dumps(config),
                   "--seconds", str(args.seconds), "--warmup", str(args.warmup),
                   "--start", str(args.start)]
        if args.modules:
            command += ["--modules", args.modules]
        try:
            outcome = subprocess.run(command, capture_output=True, text=True,
                                     timeout=args.seconds + args.warmup + 30)
        except subprocess.TimeoutExpired:
            return {"config": config, "valid": False, "error": "trial timed out"}
        if outcome.returncode:
            return {"config": config, "valid": False, "error": outcome.stderr[-2000:]}
        return json.loads(outcome.stdout)

    with destination.open("w") as log:
        for iteration, config in enumerate(itertools.chain([baseline], itertools.cycle(candidates)), 0):
            result = measure(config)
            improvement = (result.get("valid") and
                (best is None or result["cpu_pct"] < best["cpu_pct"] * 0.99))
            if improvement:
                # Confirm apparent gains; a single favorable sample is noisy.
                samples = [result, measure(config), measure(config)]
                if all(sample.get("valid") for sample in samples):
                    median_cpu = statistics.median(sample["cpu_pct"] for sample in samples)
                    result = min(samples, key=lambda sample: abs(sample["cpu_pct"]-median_cpu)).copy()
                    result["samples"] = samples
                    improvement = best is None or median_cpu < best["cpu_pct"] * 0.99
                else:
                    result["samples"] = [sample.copy() for sample in samples]
                    result["valid"] = False
                    improvement = False
            result["iteration"] = iteration
            if improvement:
                best = result
                stale = 0
            elif iteration:
                stale += 1
            result["stale_iterations"] = stale
            result["accepted"] = bool(improvement)
            result["best_cpu_pct"] = best["cpu_pct"] if best is not None else None
            log.write(json.dumps(result) + "\n")
            log.flush()
            print(json.dumps(result), flush=True)
            if stale >= args.patience:
                break
        summary = {"best": best, "stale_iterations": stale,
                   "stop_reason": "patience" if stale >= args.patience else "candidates_exhausted",
                   "iterations": iteration + 1, "threshold_relative_pct": 1.0}
        if args.modules:
            manifest = Path(args.modules)/"manifest.json"
            if manifest.is_file():
                summary["source_hashes"] = json.loads(manifest.read_text())
        destination.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--start", type=float, default=2100)
    parser.add_argument("--patience", type=int, default=50)
    parser.add_argument("--poll-fps", type=float, default=60)
    parser.add_argument("--compact", action="store_true", help="use the compact web preview")
    parser.add_argument("--output", type=Path, default=ROOT/".build/cpu-research/trials.jsonl")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--config", default="{}")
    parser.add_argument("--modules")
    parser.add_argument("--profile")
    args = parser.parse_args()
    if args.seconds <= 0 or args.warmup < 0 or args.patience <= 0 or args.poll_fps <= 0:
        parser.error("seconds, patience and poll-fps must be positive; warmup must be nonnegative")
    if args.worker:
        print(json.dumps(trial(args)))
    else:
        search(args)


if __name__ == "__main__":
    main()
