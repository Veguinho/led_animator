# Video conversion and export

[← README](../README.md) · [Stream directly to a panel](live-streaming.md)

Run these commands from the repository root after installing the
[Python dependencies](../README.md#quick-start). Install FFmpeg so `ffmpeg`
and `ffprobe` are on your PATH. The 48×48 container runner also needs Docker.

The converter center-crops to a square, averages color into a 16×16 or 48×48
RGB grid, and processes source frames incrementally. Preview images add LED
faces and glow. Physical playback uses RGB565, so it has less color precision
than the RGB888 animation data.

| Output | Purpose |
| :-- | :-- |
| `.preview.mp4` | H.264 LED preview |
| `.ledanim.npz` | Compressed RGB frames and timing |
| `.ledmap.json` | Portable RGB frame data |
| `.ledbin` | RGB565 frames for a microcontroller, produced by the exporter |

Source clips and generated outputs stay local and are ignored by Git.

## Convert a video

Place a video inside `video_clips/`, then create a 16×16 animation:

```bash
python3 led_animator.py video_clips/my_video.mp4 -o output/my_animation
```

For a 48×48 animation, use the protected Docker runner:

```bash
./run_48_container.sh video_clips/my_video.mp4 -o output/my_animation_48
```

The runner limits the conversion to 1 GiB of memory, two CPU cores, and no
network access. You can raise those limits when needed:

```bash
LED_ANIMATOR_MEMORY_LIMIT=2g LED_ANIMATOR_CPU_LIMIT=4 \
  ./run_48_container.sh video_clips/my_video.mp4 -o output/my_animation_48
```

## Play the result

```bash
python3 led_animator.py --play output/my_animation.ledanim.npz
```

## Useful options

| What you want | Command |
| :-- | :-- |
| Convert, then open the desktop player | `python3 led_animator.py input.mp4 --preview-after` |
| Create only the compact animation | `python3 led_animator.py input.mp4 --no-json --no-preview` |
| Change LED size and spacing | `python3 led_animator.py input.mp4 --led-size 28 --gap 3` |
| Make the MP4 preview lighter | `python3 led_animator.py input.mp4 --preview-fps 10` |
| Use less memory during compression | `python3 led_animator_48.py input.mp4 --batch-size 4` |
| Rebuild a preview from saved animation data | `./run_48_container.sh --preview-from output/my_animation.ledanim.npz -o output/my_animation` |

For especially long 48×48 videos, skip the larger JSON and visual preview:

```bash
./run_48_container.sh video_clips/long_video.mp4 \
  -o output/long_video_48 --no-json --no-preview --batch-size 4
```

## Use the animation data

```python
import numpy as np

with np.load("output/my_animation.ledanim.npz") as animation:
    frames = animation["frames"]  # (frame_count, grid, grid, 3), uint8
    fps = float(animation["fps"])

for frame in frames:
    # Send one row-major RGB frame to your LED board here.
    send_to_board(frame.reshape(-1, 3))
```

JSON frames follow `frames[frame][row][column][channel]`, starting at the
top-left with RGB channel values from `0` through `255`.

## Export for Arduino or ESP32

JSON is convenient for exchanging data but wasteful on a microcontroller, and
NPZ requires a ZIP/NumPy decoder. Export a 16×16 animation as RGB565 binary
instead:

```bash
python3 export_arduino.py output/my_animation.ledmap.json \
  -o output/my_animation_16x16.ledbin \
  --fps 12 \
  --header my_animation_player/my_animation.h \
  --symbol my_animation
```

The `.ledbin` contains a 24-byte little-endian header followed by row-major
RGB565 frames. The optional header embeds the same bytes in flash with
`PROGMEM` for custom offline players. The included
[streaming firmware](../cs2_16x16_player_firmware/cs2_16x16_player_firmware.ino) sketch uses live USB streaming instead, so it does
not include this header.

## Generate a procedural source clip

The optional space-bounce generator is still available; its output is not
bundled. See `python3 generate_space_bounce.py --help` for rendering options.
