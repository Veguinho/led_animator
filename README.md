# 💡 LED Animator

Live lava, audio-reactive waves, and video playback on a **32×32 RGB LED
screen** built from four 16×16 panels. Python creates the frames; an ESP32-S3
drives IO10–IO13 independently and receives frames through the existing CH340 USB
connection. Full 32×32 streaming defaults to 20 FPS on this hardware.
Live audio uses four RMT outputs and waits for each frame to finish before
acknowledging it; preloaded video retains its separate LCD_CAM driver.
Viewed from the front, the panel layout is:

```text
IO11  IO10
IO13  IO12
```

The images on the two left panels (IO11 and IO13) are mirrored horizontally.

Live audio starts in dim red at the tested 16/255 level (about 6.3%), with
manual brightness control, softened frame changes and the normal firmware brightness ceiling of 72/255.
The previews below show the original 16×16 effects. See the
[native 32×32 spectrum preview](docs/32x32-preview.md) for the current logical
screen and four-panel mapping; lava still scales its 16×16 artwork to fill the screen.

[32×32 preview](docs/32x32-preview.md) · [Quick start](#quick-start) · [Hardware](docs/hardware.md) ·
[Live streaming](docs/live-streaming.md) · [Video conversion](docs/video-conversion.md)

Choose live audio or locally buffered video with `python main.py`.
[Preloaded video mode](preloaded_video/README.md) uploads a short clip into
the board's PSRAM, then plays at 30 FPS by default without streaming each frame
over USB. Video defaults to 25% pixel intensity and 180 ms of temporal smoothing.
Its buffer holds up to about 102 seconds at 30 FPS. Switching modes
installs the corresponding firmware.
Video loading now uses a tested **2,000,000-baud** connection (about 129 KB/s
of measured payload throughput); firmware flashing remains at 115200 baud.

```bash
python main.py audio
python main.py video video_clips/clip.mp4 --seconds 10
```

## See it in motion

<table>
  <tr>
    <td align="center" width="50%">
      <a href="docs/assets/lava-lamp.mp4">
        <img src="docs/assets/lava-lamp.gif" width="258" alt="16×16 lava-lamp simulation with glowing red and orange fluid rising and curling">
      </a>
      <br><strong>Lava lamp</strong>
      <br>Heat, buoyancy, and fluid motion.
      <br><a href="docs/assets/lava-lamp.mp4">Watch the MP4</a>
    </td>
    <td align="center" width="50%">
      <a href="docs/assets/audio-spectrum.mp4">
        <img src="docs/assets/audio-spectrum.gif" width="258" alt="16×16 rainbow spectrum with mirrored frequency bars reacting to recorded Mac system audio">
      </a>
      <br><strong>Live audio spectrum</strong>
      <br>Rainbow frequency bars follow the music.
      <br><a href="docs/assets/audio-spectrum.mp4">Watch with sound</a>
    </td>
  </tr>
  <tr>
    <td align="center" colspan="2">
      <a href="docs/assets/audio-adaptive.mp4">
        <img src="docs/assets/audio-adaptive.gif" width="258" alt="16×16 Adaptive spectrum changing color and brightness with the energy of live Mac system audio">
      </a>
      <br><strong>Adaptive audio spectrum</strong>
      <br>Warm, vivid colors for energetic passages; softer Ocean colors for quieter sections.
      <br><a href="docs/assets/audio-adaptive.mp4">Watch with sound</a>
    </td>
  </tr>
</table>

The audio previews use real Mac system audio recorded during playback.
Adaptive captures the running visualizer's live frames at 60% brightness and
20% slowdown. Both audio MP4s include the recorded sound; the inline GIFs are
silent. The lava is a seeded fluid simulation.
[Record or rebuild the previews](docs/development.md#readme-previews).

## What you can run

| Mode | Input | Command after setup |
| :-- | :-- | :-- |
| Lava lamp | Procedural fluid simulation | `python3 lava_lamp_stream.py --clear-on-exit` |
| Audio wave | macOS system audio | `python3 system_audio_visualizer.py --style wave --clear-on-exit` |
| Audio spectrum | macOS system audio | `python3 system_audio_visualizer.py --style spectrum --clear-on-exit` |
| Video stream | Local video decoded with FFmpeg | `python3 stream_arduino.py video_clips/my_video.mp4 --loop --clear-on-exit` |
| Video conversion | Local video | `python3 led_animator.py video_clips/my_video.mp4 -o output/my_animation` |

## Quick start

Use Python 3.10 or newer. Live system audio requires **macOS 14.2+** and Apple's
Command Line Tools. Video conversion and preview generation require **FFmpeg**.

```bash
git clone https://github.com/Veguinho/led_animator.git
cd led_animator
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

For physical playback, follow the [wiring and firmware setup](docs/hardware.md)
for an ESP32-S3 and four 16×16 WS2812B panels with external 5 V power and
common ground. Connect the controller's existing USB port. After installing
the firmware prerequisites, upload and start the audio wave:

```bash
./start.sh --style wave
```

Allow **System Audio Recording Only** for your terminal when macOS asks.
Screen recording permission is not needed.
For later runs, skip the upload with `./start.sh --no-upload --style wave`.
Running `./start.sh` without a style selects the spectrum.

To switch to lava, stop the audio stream with Ctrl+C, then run:

```bash
python3 lava_lamp_stream.py --clear-on-exit
```

No panel is needed to [convert videos](docs/video-conversion.md) or
[render the documentation previews](docs/development.md#readme-previews).

## Documentation

- [Hardware and firmware](docs/hardware.md): wiring, power, board setup, upload.
- [32×32 mode preview](docs/32x32-preview.md): native spectrum rendering and panel mapping.
- [Live streaming](docs/live-streaming.md): lava, wave, spectrum, video, and troubleshooting.
- [Video conversion](docs/video-conversion.md): 16×16 and 48×48 workflows, Docker, data formats, Arduino export.
- [Development](docs/development.md): tests, project structure, and preview generation.

Only the small documentation previews are versioned. Source videos, exported
animations, and generated firmware animation headers stay out of Git.
