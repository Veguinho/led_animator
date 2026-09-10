# 💡 LED Animator

Live lava, audio-reactive waves, and video playback on a **16×16 RGB LED
panel**. Python creates the frames; an ESP32-S3 drives the LEDs over USB.
The video converter also supports **48×48** grids.

[Quick start](#quick-start) · [Hardware](docs/hardware.md) ·
[Live streaming](docs/live-streaming.md) · [Video conversion](docs/video-conversion.md)

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
      <a href="docs/assets/audio-wave.mp4">
        <img src="docs/assets/audio-wave.gif" width="258" alt="16×16 blue-to-red oscilloscope wave growing and fading with changing audio levels">
      </a>
      <br><strong>Live audio wave</strong>
      <br>Wave shape and glow follow the sound.
      <br><a href="docs/assets/audio-wave.mp4">Watch the MP4</a>
    </td>
  </tr>
</table>

These looping previews use the actual live renderers. The wave is driven by
synthetic tones with changing volume; the lava is a seeded fluid simulation.
They are recorded demonstrations, not a live connection to a panel. Click
for the 30 FPS MP4 versions. [Rebuild the previews](docs/development.md#readme-previews).

## What you can run

| Mode | Input | Command after setup |
| :-- | :-- | :-- |
| Lava lamp | Procedural fluid simulation | `python3 lava_lamp_stream.py --clear-on-exit` |
| Audio wave | macOS system audio | `python3 system_audio_visualizer.py --style wave --clear-on-exit` |
| Audio spectrum | macOS system audio | `python3 system_audio_visualizer.py --style spectrum --clear-on-exit` |
| Video stream | Local video decoded with FFmpeg | `python3 stream_arduino.py video_clips/my_video.mp4 --loop --clear-on-exit` |
| Video conversion | Local video | `python3 led_animator.py video_clips/my_video.mp4 -o output/my_animation` |

## Quick start

Use Python 3.10 or newer. Live system audio requires **macOS 13+** and Apple's
Command Line Tools. Video conversion and preview generation require **FFmpeg**.

```bash
git clone https://github.com/Veguinho/led_animator.git
cd led_animator
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

For physical playback, follow the [wiring and firmware setup](docs/hardware.md)
for an ESP32-S3 and a 16×16 WS2812B matrix with a separate 5 V supply and
common ground. After installing the firmware prerequisites, upload and
start the audio wave:

```bash
./start.sh --style wave
```

Allow **Screen & System Audio Recording** for your terminal when macOS asks.
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
- [Live streaming](docs/live-streaming.md): lava, wave, spectrum, video, and troubleshooting.
- [Video conversion](docs/video-conversion.md): 16×16 and 48×48 workflows, Docker, data formats, Arduino export.
- [Development](docs/development.md): tests, project structure, and preview generation.

Only the small documentation previews are versioned. Source videos, exported
animations, and generated firmware animation headers stay out of Git.
