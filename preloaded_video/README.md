# Preloaded video mode

> Compatibility note: the active 48×48 mode now lives in
> [`mp4_player/`](../mp4_player/README.md). Use `python main.py mp4 VIDEO`.
> This document retains historical implementation notes for the older package
> name and may describe the previous 32×32 panel layout.

This mode uploads a short video through the existing CH340 USB socket, then
plays it from ESP32-S3 PSRAM. Serial bandwidth affects the initial upload,
but does not set the local playback rate. Playback defaults to **30 FPS** on
the same four 16×16 panels, with IO10–IO13 arranged as the existing 2×2 grid.
The live audio firmware and renderer remain separate.

From the project root, with the virtual environment active:

```bash
python main.py                         # interactive audio/video menu
python main.py audio                   # install audio firmware, start wave at 20 FPS
python main.py video video_clips/clip.mp4 --seconds 10
```

Selecting a mode compiles and uploads its firmware using the existing USB
port at the tested 115200-baud upload rate. The launcher stops only this
checkout's previous audio/video worker before accessing the board.
Changing between audio and video requires flashing the matching firmware.
This replaces the installed program; it does not change your source files.

Video transfers now default to **2,000,000 baud**. The firmware boots at
230400, acknowledges a speed change, then requires confirmation at the new
rate. An unconfirmed change reverts to 230400 after 1.5 seconds. The player
also probes the supported rates when reconnecting. Use `--baud 230400` for
the original transfer speed. Older video firmware needs one firmware upload
before using the new default.

Once the video firmware is installed, additional clips need no firmware flash:

```bash
python main.py video video_clips/clip.mp4 --start 5 --seconds 10 --no-upload
python main.py audio --no-upload       # only when audio firmware is installed
```

Use `--port /dev/cu.usbserial-1420` if automatic detection finds multiple
controllers. `--no-upload` always means the correct firmware is already on
the board; the video protocol deliberately differs from the audio protocol
to prevent accidentally displaying upload bytes as live frames. The 32×32
video protocol also uses a distinct `PV32` signature so old 48×48 firmware
cannot accept clips with the wrong frame dimensions.

## Capacity and preparation

The detected ESP32-S3 has **8 MB OPI PSRAM**. The video firmware enables
`PSRAM=opi` and reserves up to **6 MiB** for video after initializing the LED
driver. It reports its actual available capacity to the uploader. It does
not fall back to internal RAM when PSRAM is missing.

Each 32×32 RGB565 frame uses 2048 bytes. The maximum 6 MiB budget holds
**3072 frames**, about **51.2 seconds at 60 FPS** or **102.4 seconds at 30 FPS**.
More complex images take the same space. Clips exceeding the budget are
rejected before flashing; select a shorter segment with `--start`/`--seconds`.
No SD card or extra USB connector is required.

FFmpeg center-crops the video, resizes to 32×32, and resamples to the selected
frame rate. A lower-rate source repeats frames at 60 FPS; this does not
invent intermediate motion. Gamma defaults to 2.2, matching live video.
Video sound is not played by this mode.

For gentler playback, preparation defaults to `--brightness 0.25` and
`--smooth-ms 180`. Brightness scales the LED pixel intensities before RGB565
encoding; smoothing blends changes over time, including the loop boundary.
These settings also apply when selecting video from the main menu.
The firmware's separate brightness and power limits still apply.

```bash
python main.py video docs/assets/audio-spectrum.mp4 --no-upload
python main.py video video_clips/clip.mp4 --brightness 0.15 --smooth-ms 250 --no-upload
```

```bash
python main.py video video_clips/clip.mp4 --seconds 10 --fps 60 --prepare-only
```

`--prepare-only` validates conversion and the maximum capacity without
stopping the running audio stream or touching the board. Preparation reports
the duration, number of frames, byte count, and minimum upload time.
On the previous 48×48 setup at 2,000,000 baud, measured payload throughput is about **129 KB/s** with
4096-byte chunks and acknowledgements. A ten-second 30 FPS clip takes about
5 seconds to transfer; a full buffer takes about 49 seconds. This is a
one-time wait before each newly loaded clip. Preparation reports the wire
speed minimum; the player reports the actual elapsed transfer time afterward.

That earlier hardware benchmark transferred three 2,359,296-byte random clips with
whole-clip CRC verification and zero retries. It keeps the LEDs blank:

```bash
python preloaded_video/benchmark_serial.py --baud 2000000 --rounds 3
```

This measurement is for preloading, with LED playback stopped. **It does not
yet establish continuous 30 FPS streaming**: 32×32 RGB565 at 30 FPS requires
61,440 payload bytes/s, plus room for concurrent rendering. Playback from
the fully loaded buffer still runs at 30 FPS.

## Playback and buffering

After every chunk and the complete clip pass CRC32 verification, playback
starts from PSRAM. The firmware decodes RGB565 into the four panel buffers
and uses parallel LCD_CAM DMA with each channel explicitly assigned to
`fl::Bus::LCD_CLOCKLESS`. With the installed FastLED 3.10.5, the legacy
`addLeds<..., Bus>` template only links that driver and still binds its
controllers to RMT; the video firmware uses `ChannelConfig` to select LCD.
It waits for DMA
before replacing display data and schedules frames against the ESP32's
microsecond clock. When a deadline is missed, it advances to the correct
frame instead of slowing the whole clip. The host reports the actual frame
submission rate and missed deadlines while monitoring.

Historical test on the previous 48×48 layout (2026-09-12): the 16-second `docs/assets/audio-spectrum.mp4`
clip was converted to 960 frames at 48×48 and loaded successfully (4,423,680
bytes). With explicit LCD channel selection, the board reported about
**45.5 frame submissions per second**, with missed deadlines at the 60 FPS
target. This verifies buffered playback, but **60 FPS is not yet achieved**.
The source is 30 FPS, so conversion repeats source frames. Counters measure
firmware submissions, not an optical measurement of the panels.

The existing
brightness (24/255) and estimated 2 A total software power budget are retained.
They may keep the four-panel screen dim until the supply and wiring are sized
for a higher budget. See [hardware setup](../docs/hardware.md).

Playback loops by default. Ctrl-C in the monitoring program sends STOP and
blanks the LEDs. `--once` plays once and holds the last displayed image.
`--detach` closes the host program after starting playback; the board keeps
playing independently. Keep the board powered if disconnecting USB.

**The clip is volatile:** reset, power loss, or another firmware upload clears
PSRAM. The firmware does not automatically play an old clip after reboot.
A failed or interrupted transfer cannot start a partially loaded video;
rerun the uploader to begin a fresh transfer.

## Files and build

To check every LED with a native 32×32 clip:

```bash
python preloaded_video/make_test_clip.py
python main.py video video_clips/panel_test_32x32.mp4 --no-upload
```

This 12-second, 30 FPS loop fades through full-screen red, green, blue and
white, then highlights all four panels in reading order (IO11, IO10, IO13, IO12).
The default dimming and smoothing apply. Every pixel receives all three
color channels; a panel remaining dark through the color washes is not
explained by empty areas in this video. The expected panel positions are:

```text
IO11  IO10
IO13  IO12
```

The images on IO11 and IO13 are mirrored horizontally, matching live audio.

For a gentle full-screen color fade with no white phase or panel highlights:

```bash
python preloaded_video/make_test_clip.py --pattern fade
python main.py video video_clips/color_fade_32x32.mp4 --led-gamma 1 --brightness 0.15 --smooth-ms 250 --no-upload
```

This 12-second loop uses four-second color transitions across all 1024 LEDs.
The source keeps the total RGB intensity constant; the playback command
applies dimming and smoothing before RGB565 encoding.

- `player.py`: video conversion, chunk upload, playback commands and monitoring.
- `make_test_clip.py`: reproducible full-screen color and panel coverage test.
- `preloaded_video_firmware/`: independent Arduino sketch and buffer/timing logic.
- `../main.py`: common mode selector; `../device_modes.py`: scoped process cleanup
  and Arduino CLI calls.

The firmware uses the same Arduino-ESP32 3.3.11 and FastLED 3.10.5 installation
as live mode. To compile without flashing:

```bash
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:CDCOnBoot=default,UploadSpeed=115200,PSRAM=opi' \
  preloaded_video/preloaded_video_firmware
```
