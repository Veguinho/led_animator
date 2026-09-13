# Development

[← README](../README.md)

## Tests

From the repository root, with the virtual environment active:

```bash
python3 -m unittest discover -s tests -v
```

## Project map

| File | Role |
| :-- | :-- |
| `led_animator.py` / `led_animator_48.py` | Video conversion, saved data, and LED previews |
| `export_arduino.py` | RGB565 binary and optional C header export |
| `stream_arduino.py` | Video streaming, serial transport, and board detection |
| `lava_lamp_stream.py` | Fluid solver and lava streaming |
| `system_audio_visualizer.py` | Audio-wave and spectrum renderers |
| `macos_system_audio.swift` | Native macOS system-audio capture helper |
| `macos_system_audio.plist` | Audio capture permission description |
| `audio_palette_controls.py` / `.html` | Local palette controls and live panel preview |
| `start.sh` | Firmware upload and audio-stream launcher |
| `scripts/install_macos_app.py` | Mac app installer and optional Dock shortcut |
| `scripts/launch_audio_visualizer.py` | App entry point and duplicate-stream lock |
| `Dockerfile` / `run_48_container.sh` | Resource-limited 48×48 video conversion |
| `cs2_16x16_player_firmware/` | ESP32-S3 live receiver and physical panel mapping |
| `scripts/render_readme_previews.py` | Reproducible documentation demos |
| `scripts/render_32x32_preview.py` | Reproducible native 32×32 spectrum image |
| `scripts/record_adaptive_preview.py` | Record the running Adaptive preview and Mac system audio |
| `tests/` | Audio, palette, lava, video conversion, export, and USB protocol tests |

Only application code, launchers, firmware, documentation assets, and tests
are versioned. Keep source videos in `video_clips/` and generated media in
`output/`; both are ignored. The local `.venv/` contains installed dependencies,
and `.build/` holds the compiled capture helper and the app's runtime lock.

## README previews

Regenerate the native 32×32 spectrum image without audio hardware or a board:

```bash
python3 scripts/render_32x32_preview.py
```

This writes `docs/assets/spectrum-32x32.png` from a deterministic multi-tone
signal using the live Sunset spectrum renderer. It is the image shown in the
[32×32 preview guide](32x32-preview.md).

The animated previews use recorded audio and the original 16×16 presentation.

Install the usual Python requirements and FFmpeg. Use a fresh audio recording,
or extract the soundtrack from the existing preview:

```bash
mkdir -p output
ffmpeg -i docs/assets/audio-spectrum.mp4 -vn output/system-audio.wav
```

Then rebuild the previews:

```bash
python3 scripts/render_readme_previews.py --audio-file output/system-audio.wav
```

This regenerates the four lava and rainbow files in `docs/assets/`: one 16-second,
30 FPS H.264 MP4 and one looping 12 FPS GIF for each mode. GIFs display inline
in the README; each links to its MP4. The frames use the existing circular
LED renderer at 258×258 pixels.

Lava uses `LavaLampFluid` with seed 2026, the default 48×48 solver, and a
15-second warmup. The spectrum preview feeds an actual audio recording,
decoded to mono 48 kHz, into `AudioVisualizer("spectrum")` with an explicit
rainbow palette, full brightness, no slowdown, and default sensitivity. The trailing FFT windows follow the recorded
audio timeline, and the MP4 includes that same soundtrack. The GIF is silent.
The checked-in spectrum is a screen recording of that rendered spectrum,
driven by Mac system audio captured during playback on September 10, 2026.
The recording is cropped to the preview window and paired with the captured
soundtrack. Rebuilding produces the same renderer's frames directly.

Provide at least 16 seconds of real recorded audio; synthetic tones are no
longer used. Keep the original recording in ignored `output/`. To regenerate
just the spectrum, add `--audio-only`. To regenerate just lava without an
audio recording, use `--lava-only`.

### Record Adaptive

Start the live spectrum, select **Adaptive**, and play music on the Mac.
With the virtual environment active, run in another terminal:

```bash
python3 scripts/record_adaptive_preview.py
```

The recorder samples the running visualizer's live preview for 16 seconds at
30 FPS and records the system audio through a separate Core Audio tap.
It renders those captured frames as circular LEDs in
`docs/assets/audio-adaptive.mp4` with sound and a looping 12 FPS
`docs/assets/audio-adaptive.gif`. The README recording uses 60% brightness
and 20% slowdown. Keep the settings unchanged throughout recording.
Use `--controls-port` if the live controls are on a port other than 8765.
The source WAV, frames, palettes, and settings stay in ignored `output/`.

These are software previews: the physical panel also quantizes frames to
RGB565 and applies the firmware brightness and power limits.

Rendering from an existing audio file needs no board or capture permissions.
Recording new system audio requires macOS audio capture permission.
Review the resulting motion, audio, and file sizes before committing.
Keep future previews short; ordinary generated media belongs in the ignored
`output/` directory. Local input videos belong in `video_clips/`.
