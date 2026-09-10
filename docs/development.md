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
| `start.sh` | Firmware upload and audio-stream launcher |
| `cs2_16x16_player_firmware/` | ESP32-S3 live receiver and physical panel mapping |
| `scripts/render_readme_previews.py` | Reproducible documentation demos |

## README previews

Install the usual Python requirements and FFmpeg, then run:

```bash
python3 scripts/render_readme_previews.py
```

This regenerates the four small files in `docs/assets/`: one 16-second,
30 FPS H.264 MP4 and one looping 12 FPS GIF for each mode. GIFs display inline
in the README; each links to its MP4. The frames use the existing circular
LED renderer at 258×258 pixels.

Lava uses `LavaLampFluid` with seed 2026, the default 48×48 solver, and a
15-second warmup. The audio preview feeds synthetic tones at 48 kHz into
`AudioVisualizer("wave")` with default sensitivity. It exercises quiet and
loud passages without recording music or accessing system audio.

These are software previews: the physical panel also quantizes frames to
RGB565 and applies the firmware brightness and power limits.

No board, serial connection, macOS capture permissions, or source video is
needed. Review the resulting motion and file sizes before committing.
Keep future previews short; ordinary generated media belongs in the ignored
`output/` directory. Local input videos belong in `video_clips/`.

The old showcase clips, binary animation, and generated
`cs2_16x16_player/cs2_16x16_animation.h` have been removed from project history.
After that history rewrite, existing clones should be replaced with a fresh
clone after saving local work; merging an old branch can reintroduce those
assets.
