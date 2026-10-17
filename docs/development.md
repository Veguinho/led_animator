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
| `scripts/render_readme_previews.py` | Shared preview exporter and lava/spectrum demos |
| `scripts/render_all_mode_previews.py` / `capture_doom_preview.cjs` | Complete README gallery from actual renderers |
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

The README gallery has eleven eight-second animated previews: lava, wave,
rainbow spectrum, Adaptive spectrum, original video, audio-textured video,
Slow → fast, preloaded video, Doom Fire, Doom, and projectM. Video conversion
and YouTube use the same display pipeline as the video preview.

Install the Python requirements and FFmpeg. For the complete gallery, also
build projectM once with `python3 scripts/setup_projectm.py` and install
Node.js, Chrome and `playwright-core`. The browser capture uses
`.build/ui-check/node_modules/playwright-core` by default; install it with:

```bash
npm install --prefix .build/ui-check playwright-core
```

Reuse the soundtrack already included in the existing preview, then rebuild:

```bash
mkdir -p output
ffmpeg -i docs/assets/audio-spectrum.mp4 -vn output/system-audio.wav
python3 scripts/render_all_mode_previews.py --audio-file output/system-audio.wav
```

The script renders at 24 FPS, exports a looping 12 FPS GIF plus H.264 MP4 for
each mode, and muxes the recording into each audio-reactive MP4. A seeded lava
simulation supplies the source video, passed through the real FFmpeg and
RGB565 pipelines. Use `--video-file VIDEO.mp4` for another local source.
The preloaded example uses the real 48×48 clip preparation, default 25%
intensity and 180 ms smoothing. Wave, spectrum, Adaptive, Doom Fire and video
effects run at 48×48 with 60% shared brightness and 20% slowdown; lava keeps
its original 16×16 artwork. The projectM capture uses the native engine and
**cope + flexi - colorful marble (ghost mix)** preset, after a two-second
warmup. Use `--projectm-preset PATH` to select another installed preset.

Doom capture starts an isolated local server and records the actual browser
engine canvas during keyboard gameplay. It does not connect to the LED board
or change the running app. Set `DOOM_PLAYWRIGHT_MODULE` and `DOOM_CHROME` to
use other Playwright/Chromium installations. Its MP4 is silent.

Use `--seconds 1` through `--seconds 16` or `--fps 12` through `--fps 30` to
adjust the gallery. The default eight-second clips keep the versioned assets
small. All source recordings and temporary input videos remain outside Git.

For only the original lava and spectrum renderer demos, use
`python3 scripts/render_readme_previews.py --audio-file output/system-audio.wav`
with a recording at least 16 seconds long. `--lava-only` needs no audio;
`--audio-only` rebuilds only the rainbow spectrum.

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
