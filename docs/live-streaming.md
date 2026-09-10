# Live streaming

[← README](../README.md) · [Hardware and firmware](hardware.md)

Run commands from the repository root with the virtual environment active.
Upload the streaming firmware once and run only one streamer at a time.
Ctrl+C stops a stream; `--clear-on-exit` also turns off the LEDs.

## Stream a video

The streaming sketch does not store an animation in flash. Upload
[the streaming sketch](../cs2_16x16_player_firmware/cs2_16x16_player_firmware.ino) once, connect the
ESP32 by USB, and run:

```bash
python3 -m pip install -r requirements.txt
python3 stream_arduino.py video_clips/my_video.mp4 --loop
```

The Mac decodes the MP4 with FFmpeg, center-crops it, converts every selected
frame to a 16×16 RGB565 image, applies an LED intensity curve, and sends the
512-byte frames at 230400 baud. Pure black switches the LED off; colors near
black keep their hue but use progressively less PWM power instead of being
shown as equally bright colors.
Playback defaults to 30 FPS for reliable CH340 USB-serial operation.
Nothing is written to a generated `.h` file. With the default `--port auto`,
the streamer ignores Bluetooth devices and waits up to 30 seconds for a USB
serial board, so it can be started before disconnecting and reconnecting the
cable. Change the wait with `--port-wait 60`, or disable it with
`--port-wait 0`. If more than one USB serial controller is connected, list and
select the board explicitly:

```bash
python3 stream_arduino.py --list-ports
python3 stream_arduino.py video_clips/my_video.mp4 \
  --port /dev/cu.usbserial-1410 --fps 30 --loop
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

The default 48×48 simulation grid is averaged down to the physical 16×16
matrix. Use `--seed` for a repeatable flow pattern, `--fps` for the refresh
rate, or `--port` when more than one USB controller is connected.

## System audio: wave and spectrum

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

The default is a rainbow oscilloscope wave. For mirrored frequency bars, use:

```bash
python3 system_audio_visualizer.py --style spectrum
```

Once streaming starts, a **Live palette** panel opens in your browser at
`http://127.0.0.1:8765`. Pick rainbow, blue-to-red, sunset, ocean, neon, or fire;
edit up to six colors with color pickers or hex codes; combine them as a smooth
gradient or solid bands; reverse their order; and adjust brightness and
saturation. All changes apply to the next audio frame in either style, with a
live 16×16 preview. **Reset defaults** restores the rainbow palette and full
animation speed. Settings are not saved between runs.

Choose **Moving rainbow** for a continuous rainbow that travels smoothly
from left to right across the X axis. It works with both wave and spectrum.
The colors wrap around without a jump and complete one cycle in eight seconds
at full speed. **Slowdown** slows both the animation and the color movement;
**Reverse direction** makes the colors travel right to left. Brightness and
saturation still apply, and silent audio stays dark. This preset always uses
a smooth gradient. The original **Rainbow** preset keeps its colors stationary.

The **Slowdown** slider reduces how often the animation advances while keeping
the configured LED output rate. Slide right to increase the slowdown and left to
decrease it, or focus the slider and use the arrow keys. Home selects 0%; End
selects 95%. Intermediate frames blend smoothly between the new animation
frames, and the preview shows those blended frames too.

At the default 30 FPS, 50% slowdown creates 15 new animation frames per second
and blends between them to keep sending 30 frames. At a configured 60 FPS,
the same setting would create 30 new frames and send 60. The current USB setup
still defaults to 30 FPS for reliable streaming; the slider does not change the
serial speed or firmware. A higher slowdown makes reactions more gradual,
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

Click **Refresh app** at the top of the controls page to restart the visualizer
and reload the page in the same tab. The button briefly pauses the LEDs, closes
the audio and USB connections, then loads the current code and reconnects.
Live edits reset to the startup settings. The Dock launcher remains active to
prevent a second stream from starting during the refresh. If the app has stopped
completely, launch it from the Dock again.

Both styles follow the audio level in dBFS: loud passages grow and glow more,
while quiet passages stay smaller and dimmer.

The default audio sensitivity multiplier is `--sensitivity 1.5`. Raise it further
for especially quiet sources, or select a controller explicitly
with `--port /dev/cu.usbserial-1410` when multiple boards are connected.

To compile and upload the ESP32 sketch and immediately start the live spectrum,
complete the [firmware prerequisites](hardware.md#firmware-installation), then run:

```bash
./start.sh
```

The launcher auto-detects the USB port and uses the ESP32-S3 board profile. Use
`./start.sh --no-upload` to restart only the Python stream, or pass another
board profile with `--fqbn` when needed. All remaining options, such as
`--sensitivity 2`, are forwarded to the visualizer.

## Clickable Mac app and Dock shortcut

Install the launcher in your personal Applications folder and pin it to the Dock:

```bash
.venv/bin/python scripts/install_macos_app.py --dock
```

Connect the board by USB, then click **LED Audio Visualizer** in the Dock (or
double-click `~/Applications/LED Audio Visualizer.app`). A Terminal window starts
the spectrum with the default rainbow palette, and the live color controls open
in your browser once streaming begins. Play audio on the Mac to animate the LEDs.
If macOS requests audio recording access, allow Terminal in **System Settings →
Privacy & Security → Screen & System Audio Recording → System Audio Recording
Only**. Screen recording access is not required.

Keep the Terminal window open while using the visualizer. Press **Control-C** in
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
