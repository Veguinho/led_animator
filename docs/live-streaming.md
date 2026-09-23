# Live streaming

[← README](../README.md) · [Hardware and firmware](hardware.md)

Run commands from the repository root with the virtual environment active.
Upload the streaming firmware once and run only one streamer at a time.
Ctrl+C stops a stream; `--clear-on-exit` also turns off the LEDs.
The current firmware uses nine panels and the existing CH340 USB port.
All streamers default to `--display-size 48`; use `--display-size 16` only
with the old single-panel firmware and `--baud 230400`. The launcher and Python commands
default to 30 FPS by overlapping 48×48 UART reception with hardware-timed LED output.
Upload the updated live firmware once before using these faster defaults.
The terminal reports measured FPS and skipped frames every five seconds. The
panel still receives 30 frames per second, but live audio analysis and animation
rendering default to 15 updates per second to favor Mac performance. Each
encoded frame is reused once, avoiding a second FFT, redraw, and RGB565 encode.
Use `--render-fps 30` only when maximum LED motion is more important than CPU
usage.
Acknowledgement timeouts default to 0.2 seconds so an occasional lost reply
does not pause playback for a full second; retries retain CRC validation and
duplicate-frame protection. Use `--timeout 1` if a slower host needs more time.

## Stream a video

The streaming sketch does not store an animation in flash. Upload
[the streaming sketch](../cs2_16x16_player_firmware/cs2_16x16_player_firmware.ino) once, connect the
ESP32 by USB, and run:

```bash
python3 -m pip install -r requirements.txt
python3 stream_arduino.py video_clips/my_video.mp4 --fps 30 --loop
```

The Mac decodes the MP4 with FFmpeg, center-crops it, converts every selected
frame to a 48×48 RGB565 image, applies an LED intensity curve, and sends the
4608-byte frames over CH340/UART at 2,000,000 baud. Pure black switches the LED off; colors near
black keep their hue but use progressively less PWM power instead of being
shown as equally bright colors.
Direct video playback defaults to 30 FPS, or the source frame rate if lower.
Nothing is written to a generated `.h` file. With the default `--port auto`,
the streamer ignores Bluetooth devices and waits up to 30 seconds for a USB
serial board, so it can be started before disconnecting and reconnecting the
cable. Change the wait with `--port-wait 60`, or disable it with
`--port-wait 0`. If more than one USB serial controller is connected, list and
select the board explicitly:

```bash
python3 stream_arduino.py --list-ports
python3 stream_arduino.py video_clips/my_video.mp4 \
  --port /dev/cu.usbserial-1420 --fps 30 --loop
```

The default `--led-gamma 2.2` gives dark pixels a strong intensity falloff.
Use a lower value such as `--led-gamma 1.6` to reveal more shadow detail, or
`--led-gamma 1` to send the original RGB channel levels without the curve.

The default timing drops late frames to keep the video clock accurate. Add
`--no-drop` if every frame matters more than real-time speed, or
`--clear-on-exit` to turn off the panel when playback stops.

## Lava lamp

The lava-lamp mode runs a small Eulerian fluid simulation with advection,
pressure projection, viscosity, vorticity, cooling, and thermal buoyancy. It
renders red edges, orange bodies, and yellow-hot cores directly to the panel;
no source video is required:

```bash
python3 lava_lamp_stream.py --clear-on-exit
```

Slow the motion or reduce software brightness without changing the firmware:

```bash
python3 lava_lamp_stream.py --speed 0.75 --brightness 0.8 --clear-on-exit
```

The default 48×48 simulation grid is averaged down to the existing 16×16
artwork, then scaled to the physical 48×48 screen in 3×3 pixel blocks. Use `--seed` for a repeatable flow pattern, `--fps` for the refresh
rate, or `--port` when more than one USB controller is connected.

## System audio: wave and spectrum

Both audio styles render directly at the selected screen resolution, which
defaults to **48×48**. Waves sample all 48 columns, spectrum bars use 48 bands,
and colors, trails, and slowed curves are drawn at full resolution. To stream
the wave to the nine-panel firmware using the existing USB connection:

```bash
python3 system_audio_visualizer.py --style wave --display-size 48 --fps 30 --clear-on-exit
```

Use `--display-size 16` with the old single-panel firmware and `--baud 230400`.

The panel can also react to music, a DJ set, a browser, or anything else being
played by the Mac. This captures the digital system output directly; it does
not listen through the microphone and the speakers keep playing normally:

```bash
python3 system_audio_visualizer.py --clear-on-exit
```

On the first run, macOS asks for **System Audio Recording Only** access for
the app launching the visualizer (Terminal for the command-line launcher).
Allow it in **System Settings > Privacy & Security > Screen & System Audio
Recording > System Audio Recording Only**, then restart that app if requested.
The native helper uses Core Audio taps: it does not capture displays, windows,
or the microphone, and does not need screen recording permission. It compiles
automatically on its first run and requires macOS 14.2 or newer plus Apple's
Command Line Tools. Audio is converted to mono 48 kHz for the visualizer.

The default is a red wave with 20% slowdown and 50% manual brightness. For mirrored frequency bars, use:

```bash
python3 system_audio_visualizer.py --style spectrum
```

Once streaming starts, a **Live palette** panel opens in your browser at
`http://127.0.0.1:8765`. Pick rainbow, blue-to-red, sunset, ocean, neon, or fire;
edit up to six colors with color pickers or hex codes; combine them as a smooth
gradient or solid bands; reverse their order; and adjust brightness and
saturation. All changes apply to the next audio frame in either style, with a
live preview matching the selected display size. **Reset defaults** restores red, 20% slowdown, and 50% manual brightness. The background service saves settings between runs; direct command-line streams do so only when given `--settings-file PATH`.

Choose **Moving rainbow** for a continuous rainbow that travels smoothly
from left to right across the X axis. It works with both wave and spectrum.
The colors wrap around without a jump and complete one cycle in eight seconds
at full speed. **Slowdown** slows both the animation and the color movement;
**Reverse direction** makes the colors travel right to left. Brightness and
saturation still apply, and silent audio stays dark. This preset always uses
a smooth gradient. The original **Rainbow** preset keeps its colors stationary.

Choose **Adaptive** to follow the song's energy in either style. Energetic
passages bring bright, saturated pinks, reds, oranges, and golds with green and
cooler accents for variety. Calmer passages move toward a darker, less saturated version
of **Ocean**, preserving its blue, cyan, and teal gradient.
Adaptive smooths RMS energy and sample-peak power over 250 ms, then compares
their dBFS levels with their maxima over the last 30 seconds.
RMS carries 80% of the comparison and peak dBFS 20%,
so isolated hits cannot make a sparse passage look like a sustained chorus.
Passages around 6 dB below the reference tend toward Ocean; those within 2 dB
reach the warm end. Reference floors and a quiet-audio gate keep near-silence
from normalizing into high energy. Spectrum fullness adds color variety.
Bass and treble do not directly choose warm or cold colors.
Temperature, brightness, and saturation ease toward the current energy over
time, avoiding abrupt switches on individual beats.
**Slowdown** makes those transitions more gradual while colors keep updating
between animation frames. Brightness, saturation, and reverse still apply.
Silence gradually cools the palette while the LEDs fade out.

The **Slowdown** slider reduces how often the animation advances while keeping
the configured LED output rate. Slide right to increase the slowdown and left to
decrease it, or focus the slider and use the arrow keys. Home selects 0%; End
selects 95%. Intermediate frames blend smoothly between the new animation
frames, and the preview shows those blended frames too.

In **wave** mode, any slowdown above 0% draws a flowing sine and cosine pair.
Higher slowdown makes the strokes thicker and their motion slower, while the
music still controls their height and brightness. At 0%, wave mode returns to
the original live audio waveform.

Strong audio can reach the top and bottom LED rows in both wave and spectrum
modes. Spectrum bars have 25% more vertical reach and stay brightest at the
center, with visible edges when a band reaches full height. A faint background
uses the entire rendered image while audio plays and fades out in silence. The brightness
is controlled by the slider; audio levels still shape the animation.

At the default 30 FPS, 50% slowdown creates fifteen new animation frames per second
and blends between them to keep sending thirty frames. The slider does not change
the serial bandwidth or firmware. A higher slowdown makes reactions more gradual,
but keeps sampling current audio instead of building an audio backlog.

Set an initial slowdown from the command line, including without the browser:

```bash
./start.sh --no-upload --slowdown 50
python3 system_audio_visualizer.py --style wave --slowdown 75 --no-controls
```

The range is 0–95%: 0% keeps the original response, and 95% advances at 5% of
the output rate. Color and brightness edits still apply immediately.

Use `--no-browser` to open the printed URL yourself, `--controls-port 8766` to
choose another local port (`0` picks a free port), or `--no-controls` to run
without the interface. The panel uses Python's standard library and is available
only on this Mac. An already-running visualizer needs a restart to load the new
controls.

Click **Reconnect LEDs** at the top of the controls page after unplugging or
reconnecting the USB cable. The control page and audio capture remain running,
the stale serial handle is closed, and the app retries for up to one minute until
exactly one USB LED controller appears and acknowledges a frame. If the minute
expires, the button becomes available again; click it to begin a new one-minute
attempt. The button can also reset a connection that is still open. Palette edits
remain intact, and the Dock launcher continues preventing competing streams from
opening the same controller.

Both styles follow the audio level in dBFS: loud passages grow and glow more,
while quiet passages stay smaller and dimmer.

The default audio sensitivity multiplier is `--sensitivity 1.5`. Raise it further
for especially quiet sources, or select a controller explicitly
with `--port /dev/cu.usbserial-1420` when multiple boards are connected.

To compile and upload the ESP32 sketch and immediately start the live spectrum,
complete the [firmware prerequisites](hardware.md#firmware-installation), then run:

```bash
./start.sh
```

The launcher auto-detects the USB port and uses the ESP32-S3 board profile with UART0 and a 115200-baud upload rate. Use
`./start.sh --no-upload` to restart only the Python stream, or pass another
board profile with `--fqbn` when needed. All remaining options, such as
`--sensitivity 2`, are forwarded to the visualizer.

## Clickable Mac app and Dock shortcut

### Keep streaming in the background

Start a per-user macOS LaunchAgent that runs independently of Terminal and
automatically restarts after the audio worker exits:

```bash
.venv/bin/python audio_service.py start --port /dev/cu.usbserial-1420
```

The service starts at login and retries after exits, with a five-second minimum
between launches. Audio capture and the board handshake can add startup time.
It saves palette, brightness, and slowdown in `.build/audio-palette.json` and
writes playback/error logs to `.build/audio-service.log`. The Mac must be awake
and the board connected to stream. Open <http://127.0.0.1:8765> for controls.
Once installed, the Dock shortcut starts or reuses this service, so closing
Terminal or the browser does not stop playback.

```bash
.venv/bin/python audio_service.py status
.venv/bin/python audio_service.py stop
.venv/bin/python audio_service.py uninstall
```

`stop` disables recovery until the next start or login. `uninstall` also removes
the login service. Firmware uploads and mode switches through the project
launchers stop the service before opening the USB port. To return to automatic
audio recovery afterward, run `audio_service.py start` again. Use `--port auto`
when starting if USB port names may change and only one board is connected.

### Install the Dock shortcut

Install the launcher in your personal Applications folder and pin it to the Dock:

```bash
.venv/bin/python scripts/install_macos_app.py --dock
```

Connect the board by USB, then click **LED Audio Visualizer** in the Dock (or
double-click `~/Applications/LED Audio Visualizer.app`). A Terminal window starts
the spectrum with red, 20% slowdown, and 50% manual brightness, and the live color controls open
in your browser once streaming begins. Play audio on the Mac to animate the LEDs.
If macOS requests audio recording access, allow Terminal in **System Settings →
Privacy & Security → Screen & System Audio Recording → System Audio Recording
Only**. Screen recording access is not required.

Without the background service, keep the Terminal window open while using the visualizer. Press **Control-C** in
that window to stop streaming and clear the LEDs. Closing the browser only closes
the controls; it does not stop playback. Clicking the Dock shortcut again while
an app-launched stream is running reopens the controls without starting a second
stream. Stop any separately launched command-line streamer before using the app.

The app uses this checkout and its `.venv`; it does not bundle Python or upload
firmware. Install the firmware once using `./start.sh` if needed. Re-run the app
installer if you move the project folder. Omit `--dock` to install only the app;
you can also drag it from Finder into the Dock, as described in
[Apple's Dock guide](https://support.apple.com/guide/mac-help/mh35859/mac).

To launch the same app workflow from Terminal:

```bash
.venv/bin/python scripts/launch_audio_visualizer.py
```

## Troubleshooting

- **No board found:** run `python3 stream_arduino.py --list-ports`, check the
  USB data cable, and select a port with `--port`. Auto-detection waits 30 seconds.
- **Port busy:** stop the previous streamer or close the Arduino Serial Monitor.
- **Audio capture denied:** enable the launching app in macOS **Privacy & Security >
  Screen & System Audio Recording > System Audio Recording Only**, then restart
  that app if requested. Screen recording permission is not required.
- **Capture helper will not compile:** install Apple's Command Line Tools with
  `xcode-select --install`. Audio-only capture requires macOS 14.2 or newer.
- **Wrong LED order or orientation:** check the serpentine mapping in the
  firmware. Python sends top-left, row-major frames.

A rendered sample of each live mode appears in the README. The audio sample
uses generated tones; actual motion follows whatever is playing on your Mac.

### Manual brightness and smoothing

The brightness slider sets the software level from 0% to 100% (50% by default).
There is no automatic brightness cycle or peak normalization. The final
output stage uses a 0.15-second rise and 0.30-second fall; it does not boost dim
frames. Beats appear quickly while the gentler release avoids abrupt flashes.
Audio-reactive effects still follow the music and fade in silence.
The firmware preserves source brightness up to 255/255 and permits an
`R + G + B` total of 255 per pixel, so sparse highlights can be much brighter
while black stays fully off. FastLED still enforces the estimated 2 A
whole-panel power budget, which dims large bright areas. These are software
limits, not measurements of physical light output, and cannot prevent flashes
caused by corrupted LED signals or faulty wiring.
