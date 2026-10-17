#!/usr/bin/env python3
"""Render the complete README gallery without connecting to the LED controller."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from doom_fire import DoomFire
from export_arduino import encode_rgb565
from lava_lamp_stream import LavaLampFluid
from led_animator import map_led_intensity
from preloaded_video.player import DISPLAY_SIZE, FRAME_BYTES, prepare_clip
from projectm_controls import PRESETS, ProjectMRenderer
from render_readme_previews import DESTINATION, export
from stream_arduino import decode_rgb565_frame, iter_video_grids, open_video, probe_video
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, SAMPLE_RATE
from video_audio_texture import LiveAudioVideoTexture
from video_controls import VideoPlaybackControls
from video_motion import LiveAudioVideoMotion


def controls(preset="adaptive"):
    result = PaletteControls(size=48, slowdown=20)
    result.update(default_settings() | {
        "preset": preset, "brightness": .6, "slowdown": 20,
    })
    return result


def quantize(frame):
    return decode_rgb565_frame(encode_rgb565(frame), frame.shape[0])


def capture_doom(directory, fps, seconds):
    server = PaletteServer(PaletteControls(size=48), 0, video_library={})
    server.start()
    destination = directory / "doom.json"
    try:
        subprocess.run([
            "node", str(ROOT / "scripts/capture_doom_preview.cjs"), server.url,
            str(destination), str(fps), str(seconds),
        ], check=True, cwd=ROOT, timeout=seconds + 60)
    finally:
        server.close()
    return [np.array(frame, np.uint8).reshape(48, 48, 3)
            for frame in json.loads(destination.read_text())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-file", type=Path, required=True,
                        help="existing recording; reused for every audio-reactive preview")
    parser.add_argument("--video-file", type=Path,
                        help="optional video; defaults to a generated, seeded lava clip")
    parser.add_argument("--seconds", type=int, default=8)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--projectm-preset", type=Path,
                        default=PRESETS / "Reaction/Cloudy/cope + flexi - colorful marble (ghost mix).milk")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 16 or not 12 <= args.fps <= 30:
        parser.error("use 1–16 seconds and 12–30 FPS to keep documentation assets small")
    if not args.projectm_preset.is_file():
        parser.error("projectM presets are missing; run scripts/setup_projectm.py first")
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(args.audio_file), "-t", str(args.seconds),
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
    ], check=True, capture_output=True)
    samples = np.frombuffer(decoded.stdout, dtype="<f4")
    if (len(samples) < SAMPLE_RATE * args.seconds or not np.all(np.isfinite(samples))
            or np.max(np.abs(samples)) < 1e-5):
        parser.error("provide a recording with usable audio for the full preview duration")
    count, dt = args.seconds * args.fps, 1 / args.fps

    def window(index, length=FFT_SIZE):
        end = min(len(samples), round((index + 1) * SAMPLE_RATE / args.fps))
        result = np.zeros(length, np.float32)
        available = samples[max(0, end - length):end]
        result[-len(available):] = available
        return result

    def save(name, frames, audio=True):
        export(name, frames, args.audio_file if audio else None, fps=args.fps)

    DESTINATION.mkdir(parents=True, exist_ok=True)
    lava = LavaLampFluid(fps=args.fps, seed=2026, warmup_seconds=15)
    lava_frames = [lava.render() for _ in range(count)]
    save("lava-lamp", lava_frames, audio=False)
    for name, style, preset in [
        ("audio-wave", "wave", "sunset"),
        ("audio-spectrum", "spectrum", "rainbow"),
        ("audio-adaptive", "spectrum", "adaptive"),
    ]:
        visualizer = AudioVisualizer(style, controls=controls(preset), size=48)
        frames = []
        for index in range(count):
            # Advance the renderer's envelopes at the documented output cadence.
            with patch("system_audio_visualizer.time.monotonic", return_value=100 + index * dt):
                frames.append(quantize(visualizer.render(window(index))))
        save(name, frames)
    fire = DoomFire(48, seed=2026)
    save("doom-fire", [quantize(fire.render(window(i), dt, brightness=.6))
                       for i in range(count)])

    with tempfile.TemporaryDirectory(prefix="led-gallery-") as temporary:
        directory = Path(temporary)
        video = args.video_file
        if video is None:
            video = directory / "lava-source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", "16x16", "-r", str(args.fps), "-i", "pipe:0", "-an",
                "-c:v", "libx264", "-crf", "15", "-pix_fmt", "yuv420p", str(video),
            ], input=np.stack(lava_frames).tobytes(), check=True)
        source = open_video(video, target_fps=args.fps, size=48)
        if source.fps < args.fps:
            parser.error("choose --fps no higher than the source video's frame rate")
        iterator = source.iter_frames()
        try:
            video_frames = [decode_rgb565_frame(frame, 48) for _, frame in zip(range(count), iterator)]
        finally:
            iterator.close()
        if len(video_frames) < count:
            parser.error("the source video must cover the entire preview duration")
        save("video-playback", video_frames, audio=False)
        # Decode without intensity mapping; the streaming pipeline maps it after effects.
        iterator = iter_video_grids(video, probe_video(video).fps, args.fps, 48)
        try:
            raw = [np.frombuffer(frame, np.uint8).reshape(48, 48, 3)
                   for _, frame in zip(range(count), iterator)]
        finally:
            iterator.close()
        for name, motion in [("video-texture", False), ("video-slow-fast", True)]:
            playback = VideoPlaybackControls(video.name, args.seconds, args.fps, audio_motion=motion)
            effect = (LiveAudioVideoMotion if motion else LiveAudioVideoTexture)(
                controls(), playback, lambda length: window(0, length), size=48)
            position = 0.0
            frames = []
            for index in range(count):
                def select_frame(_):
                    return raw[min(int(position * args.fps), len(raw) - 1)]
                frame = effect.render(raw[index], window(index, 4096), dt,
                                      select_frame=select_frame)
                frames.append(quantize(map_led_intensity(frame, 2.2)))
                position = (position + dt * playback.playback_speed()) % args.seconds
            save(name, frames)
        clip = prepare_clip(video, args.fps, seconds=args.seconds)
        save("video-preloaded", [decode_rgb565_frame(clip.data[i:i + FRAME_BYTES], DISPLAY_SIZE)
                                 for i in range(0, len(clip.data), FRAME_BYTES)], audio=False)
        save("doom-game", capture_doom(directory, args.fps, args.seconds), audio=False)

    renderer = ProjectMRenderer([args.projectm_preset], 48, args.fps)
    try:
        frames = []
        started = time.monotonic()
        for index in range(count + args.fps * 2):
            audio_index = max(0, index - args.fps * 2)
            frame = renderer.render(window(audio_index, round(SAMPLE_RATE / args.fps)), 0, 1)
            if index >= args.fps * 2:
                frames.append(quantize(np.rint(frame.astype(np.float32) * .6).astype(np.uint8)))
            time.sleep(max(0, started + (index + 1) * dt - time.monotonic()))
        save("projectm", frames)
    finally:
        renderer.close()
    print(f"projectM preset: {args.projectm_preset.relative_to(PRESETS) if args.projectm_preset.is_relative_to(PRESETS) else args.projectm_preset}")


if __name__ == "__main__":
    main()
