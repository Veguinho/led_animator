# LED Animator — agent reference

Setup, controls, implementation details, and maintenance notes. The public
[README](README.md) contains the visual gallery.

Lava, audio-reactive waves and flames, MilkDrop presets, video playback,
and playable Doom on a **48×48 RGB LED screen** built from nine 16×16 panels. Python creates the frames; an ESP32-S3
drives each panel independently and receives frames through the existing CH340 USB
connection. The compressed 48×48 video path plays Wex at its original
29.97 FPS.
The live firmware routes IO9 and IO10 through RMT and the other seven panels
through LCD_CAM. Preloaded video uses a separate firmware. The live video
player opens a local **Video** tab at `http://127.0.0.1:8765/#video` for
timeline and exact-time seeking. Live Mac audio colors the video texture and
changes its brightness and saturation; the **Audio palette** tab adjusts the
colors while the video plays. In **Vídeo**, click **Desligar texture mapping**
to play with the original colors and normal video speed; **Ligar texture mapping**
restores the live audio effects. Run `python video_app.py VIDEO.mp4` to use its
browser preview without connecting the LED board.
The **Reset boards** button in the app header restarts the LED controller and
resumes playback with the current palette and video position. It is available
in live audio and video mode when the controller is connected.
Use **Reset & reload** to reset the controller and reload the running software
with the latest code. It reconnects the same browser tab and keeps the palette,
video position, pause state, and output-layer switches.
Viewed from the front, the panel layout is:

```text
IO11  IO10  IO9
IO13  IO12  IO20
IO46  IO17  IO18
```

See the [hardware guide](docs/hardware.md) for the current panel mapping.

Live audio starts in dim red at the tested 16/255 level (about 6.3%), with
manual brightness control, softened frame changes and the normal firmware brightness ceiling of 72/255.
The public README previews every display mode, including the native 48×48 effects. The
[native 32×32 spectrum preview](docs/32x32-preview.md) documents the earlier
four-panel mapping; lava still scales its 16×16 artwork.

[32×32 preview](docs/32x32-preview.md) · [Quick start](#quick-start) · [Hardware](docs/hardware.md) ·
[Live streaming](docs/live-streaming.md) · [Video conversion](docs/video-conversion.md)

Choose live audio or locally buffered video with `python main.py`.
[Preloaded video mode](preloaded_video/README.md) uploads a short clip into
the board's PSRAM, then plays at 30 FPS by default without streaming each frame
over USB. Video defaults to 25% pixel intensity and 180 ms of temporal smoothing.
Its 48×48 buffer holds up to about **45.5 seconds at 30 FPS**. Switching modes
installs the corresponding firmware.
Video loading now uses a tested **2,000,000-baud** connection (about 129 KB/s
of measured payload throughput); firmware flashing remains at 115200 baud.

```bash
python main.py audio
python main.py video video_clips/clip.mp4 --seconds 10
python main.py youtube 'https://www.youtube.com/watch?v=pUWCnazcsbU'
```

You can also paste a YouTube link in the **Vídeo** tab while live audio or video
is running. It downloads an H.264/AAC MP4 at **up to 360p** into `.build/youtube/`,
then uses the same center crop and **48×48** player, preview, seeking, looping,
and audio texture controls as local MP4s. Completed videos are reused from cache;
the current playback continues during the download. This mode accepts recorded
videos, including Shorts, rather than ongoing live broadcasts or playlists.

The **Vídeo** tab also has a local video library, available while a video is
playing. It scans MP4 files in the project folder, `video_clips/` (including
subfolders), and `.build/youtube/`. Choose a file and click **Reproduzir seleção**
to switch videos. New files appear automatically; **Atualizar lista** scans the
folders immediately. The same library is available in the board-free preview.

The **Slow → fast** tab applies music-driven playback to any video in the
downloaded-video and local library list. Select a video and click
**Play with Slow → fast** while the live audio or video app is running.
Playback scales linearly from **0.8×** at 0% energy to **1.5×** at 100%
energy (**1.15×** at 50%). Switching files in either video selector
keeps Slow → fast enabled while the mode is active.
It uses Adaptive passage-energy tracking to tint the video itself: calm music
is dim blue and purple with softer saturation at **0.8×**; energetic music
brings brighter, saturated orange and amber as playback accelerates.
Near 100% energy, yellow and gold mix with bright pink and violet. Circular
color waves follow the video's light and shadow while gently bending its texture;
the bend eases in above 82% energy and recedes as the music calms.
Maximum color intensity and **1.5×** speed require bass, mids and highs together;
an isolated loud band produces a partial response. Silence returns to dim
blue-purple at **0.8×**. The video's light and shadow remain visible, and black
pixels stay black. Brightness and saturation sliders set their upper limits.
The LED refresh rate stays steady while the video timeline follows the energy-driven speed.
The tab shows current speed, energy, a live LED preview, pause, and restart;
**Open video timeline** provides seeking. **Slowdown** in Audio palette adjusts
the smoothing of tint, brightness, saturation and speed together. This mode requires live Mac audio and the LED
board; the selected clip must be present in the local library. For a local
clip, it is also available as `python stream_arduino.py VIDEO.mp4 --audio-motion --loop`.

The **Doom Fire** tab sends classic pixel flames to the LEDs and shows their
live preview, music energy and flame height. Click **Start Doom Fire** in the
live audio or video app. Calm passages produce low blue and purple flames,
like Adaptive; energetic music supplies hotter fuel, taller red flames and
restrained yellow highlights in the hottest regions, without white highlights.
The bottom retains a blue/purple ember bed, with yellow patches during energetic
passages. Intensity spans dim tips to bright cores within the firmware's RGB
ceiling, keeping the flame contours visible on the LEDs.
Cold flames use a lifted intensity curve and RGB565-aware channel levels so
blue and purple remain visible after the controller's brightness mapping.
Black stays black, silence extinguishes the embers, and the selected brightness
ceiling still applies.
The response combines loudness with recent passage energy and bass/mid/treble
activity. Silence cools the embers to black.
Wind follows the original algorithm: random 0–2 pixel shifts to the left use
the same value for cooling. Cooling and rise rate scale from its 40 rows to
the native 48×48 grid, retaining the original flame contours.
**Brightness limit** and **Slowdown** share their settings with Audio palette;
the fire blends gradually from blue/purple to red/orange with yellow accents
over 10–90% passage energy. A separate color envelope eases the transition
over several beats during gradual builds. Sudden low-to-high energy spikes
respond on the next LED frame: a 320 ms logarithmic air pulse heats existing
flame tongues, accelerates their rise, and sweeps blue/purple into red. The
attack uses the newest 512 audio samples (10.7 ms at 48 kHz), with immediate
band-energy analysis and a 12 ms attack envelope; release remains gradual.
Live audio is sampled after frame pacing, immediately before USB output,
avoiding a queued frame of stale audio. Batched propagation preserves the
original wind and overwrite order while reducing per-frame Python work.
Slowdown also slows gradual color transitions; sudden bursts still react
immediately. The existing LED output limits remain active.
**Return to audio visualizer** restores wave or spectrum without restarting.
Switching from video releases its audio and USB connections and reuses the same
browser tab. The board-free video preview shows the tab with launch disabled.
You can also launch directly with `./start.sh --no-upload --style doom-fire`.
Based on [Filipe Deschamps’ Doom Fire Algorithm](https://github.com/filipedeschamps/doom-fire-algorithm),
with its [MIT license](docs/licenses/doom-fire-algorithm.txt).

The **Doom** tab is a keyboard-playable game using the copied open-source
Chocolate Doom engine and Freedoom Phase 2 levels/assets. Open
`http://127.0.0.1:8765/#doom` and click **Play Doom**. W/S or Up/Down move,
A/D strafe, Left/Right turn, Ctrl fires, Space opens doors, Shift runs,
1–7 select weapons, and Enter/Esc control the menus. Click the game to focus.
The engine runs locally in the browser and scales its complete image to fill the
48×48 board at **30 FPS** through the existing live firmware. The square browser
preview shows the same 48×48 image; Fullscreen fits it to the available screen.
The game has no cropped edges or letterbox bars. Doom uses a separate 30 FPS
stream while active, then restores the previous effect and its original rate.
Brightness follows Audio palette and the existing firmware output limits.
**Stop** releases the LEDs; **Play Doom** resumes the same browser game.
Leaving the tab ends that game session and restores the previous LED effect.
Lost browser connections release the display within two seconds.
The game also works in the board-free preview. All runtime files, engine source,
licenses, credits, and rebuild instructions are in [doom_game/](doom_game/README.md).
No downloads are required at play time.

YouTube support requires the Python dependencies above and a JavaScript runtime
such as Node.js or Deno. See [yt-dlp's runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
For an existing live-stream firmware installation, use `--no-upload` with
`main.py youtube`. To preview without the board:

```bash
python video_app.py --youtube 'https://www.youtube.com/watch?v=pUWCnazcsbU'
```

## projectM

The **projectM** tab runs the real projectM/MilkDrop engine with 9,795 presets,
live Mac audio, preset search, previous/next, and automatic soft transitions.
It renders at 192×192 and averages down to **48×48** for the LED panel, using
the shared brightness/saturation controls and existing USB firmware.
Build it once with `.venv/bin/python scripts/setup_projectm.py`, restart the
app, and open `http://127.0.0.1:8765/#projectm`. You can also launch with
`./start.sh --no-upload --style projectm`.
See [projectM setup and upstream credits](projectm/README.md).


## Preview generation

Every animation is an eight-second software preview from the actual renderer.
Audio-reactive examples reuse the recorded Mac system audio; their MP4s include
the recording and the inline GIFs are silent. They use 60% brightness and 20%
slowdown. Video examples use the same generated lava clip, so its original
colors, audio texture, energy-driven playback, and buffered output can be compared.
Local files and YouTube downloads share the video pipeline shown here. Video
conversion exports that same grid; it does not add another display effect.
Preloaded video uses its default 25% intensity and 180 ms smoothing at 48×48.
Lava retains its original 16×16 artwork, scaled by the live receiver.
The other effects render at 48×48; frames are quantized through RGB565 except
for the original lava artwork and Doom's browser canvas capture. The physical
panel also applies its firmware brightness and power limits.
The projectM example uses **cope + flexi – colorful marble (ghost mix)**.
[Rebuild all previews](docs/development.md#readme-previews).

## What you can run

Commands assume setup is complete and, for `--no-upload`, the matching firmware
is already installed. The browser controls are at `http://127.0.0.1:8765/`.

| Mode | Input | Launch or browser control |
| :-- | :-- | :-- |
| Lava lamp | Procedural fluid simulation | `python3 lava_lamp_stream.py --clear-on-exit` |
| Audio wave | macOS system audio | `./start.sh --no-upload --style wave` |
| Audio spectrum | macOS system audio | `./start.sh --no-upload --style spectrum` |
| Adaptive spectrum | macOS system audio | Start spectrum; choose **Adaptive** in **Audio palette** |
| Video stream / YouTube | Local video or downloaded MP4 | `python3 stream_arduino.py VIDEO.mp4 --loop --no-audio-reactive`; **Vídeo** also accepts YouTube links |
| Video + audio texture | Video and macOS system audio | `python3 stream_arduino.py VIDEO.mp4 --loop`; enable texture mapping in **Vídeo** |
| Slow → fast | Video and macOS system audio | `python3 stream_arduino.py VIDEO.mp4 --audio-motion --loop` |
| Preloaded video · 48×48 | Local clip buffered in PSRAM | `python3 main.py video VIDEO.mp4 --seconds 10` (installs separate firmware) |
| Doom Fire | macOS system audio | `./start.sh --no-upload --style doom-fire` |
| Doom | Keyboard-playable bundled game | Open **Doom** and click **Play Doom** in the running app |
| projectM / MilkDrop | Presets and macOS system audio | Build with `python3 scripts/setup_projectm.py`, then `./start.sh --no-upload --style projectm` |
| Video conversion | Local video, no board needed | `python3 led_animator_48.py VIDEO.mp4 -o output/my_animation` |

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
for an ESP32-S3 and nine 16×16 WS2812B panels with external 5 V power and
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
- [Doom game](doom_game/README.md): keyboard controls, bundled assets, source, and licenses.
- [projectM](projectm/README.md): native engine setup, presets, favorites, and credits.
- [Mac fan control](docs/mac-fans.md): optional native fan controls while the app runs.
- [Development](docs/development.md): tests, project structure, and preview generation.

Only the small documentation previews are versioned. Source videos, exported
animations, and generated firmware animation headers stay out of Git.
