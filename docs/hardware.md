# Hardware setup

[← README](../README.md) · [Live streaming](live-streaming.md)

The streaming firmware drives a **48×48 screen** made from nine 16×16
WS2812B panels. The ESP32-S3 drives seven parallel LCD_CAM lanes and two RMT
lanes (IO9 and IO10). The Mac sends
frames over the controller's existing CH340 USB-to-UART port at 1,500,000 baud;
an external regulated 5 V supply powers the LEDs. Compressed video has been
measured at 24 FPS.

## Panel placement and wiring

Viewed from the front, the nine panel data pins are:

| | Left | Center | Right |
| :-- | :-- | :-- | :-- |
| Top | IO11 | IO10 | IO9 |
| Middle | IO13 | IO12 | IO20 |
| Bottom | IO46 | IO17 | IO18 |

Every panel uses the same clockwise rotation and horizontal mirror correction.
Each panel has horizontal serpentine wiring and its own data output lane.

Each data line above goes through its own **3.3 V → 5 V level-shifter channel**,
followed by a 330–470 Ω
resistor close to that panel's DIN. Enable the used shifter outputs. Leave the
panels' DOUT connectors unconnected: each GPIO drives only its own panel.

| Connection | Wire it to |
| :-- | :-- |
| Mac USB | Existing USB socket wired through the CH340 to ESP32-S3 UART0 |
| Nine data GPIOs | Nine level-shifter inputs, as mapped above |
| External supply +5 V | Separate, suitably fused power branches to each panel and to the level shifters |
| External supply GND | Every panel GND, ESP32 GND, and level-shifter GND |
| Recommended local capacitor | 500–1000 µF across 5 V/GND at each panel's power input, with correct polarity |

The attached controller appears as `/dev/cu.usbserial-1420` on this Mac
(CH340 VID:PID `1a86:7523`). The firmware explicitly uses `Serial0`, so no
native USB connector or board replacement is needed. IO20 is one of the nine
panel data outputs. The uncompressed 48×48 payload is 4,608 bytes, which takes
about 30.8 ms on the wire at 1,500,000 baud including packet framing.
Compressed frames are normally much smaller and leave room for 24 FPS playback.

The separate preloaded-video firmware negotiates **2,000,000 baud** after
booting at 230400. Measured checksummed upload throughput is about 129 KB/s;
see [video mode](../preloaded_video/README.md). The live-streaming firmware starts directly at 1,500,000 baud.

## Power

Feed power directly from the external supply to each panel. Do not pass the
screen's power through the ESP32, USB, or one panel's small power connector.
Use wire sizes, connectors, fuses and power injection suitable for the actual
panel current and cable lengths. Keep the external +5 V off the USB-powered
ESP32's 5 V pin unless the specific board supports that arrangement.

Live frames and the startup diagnostic also clamp each pixel to a total
`R + G + B` of **64**, preserving color ratios and never boosting dim pixels.
Temporal dithering is disabled, and both limits are reapplied before every
non-black transmission. Even a valid full-white host frame is limited. This
bounds the values sent by firmware; it cannot guarantee brightness if the
data signal is corrupted after leaving the controller.

The live firmware caps channel output at **60/255** and uses an estimated **4 A total**
FastLED power budget across all nine panels. FastLED's model counts roughly
1 mA of idle draw per LED, so the previous 2 A setting was below the estimated
idle draw of a full 48×48 display and made dense scenes nearly disappear.
The budget is a software estimate, not a hardware current
limiter or a specification for a suitable supply. Size the supply using your
panel specifications/measurements, including idle consumption, before raising
`MAX_POWER_MILLIAMPS`. See [Adafruit's power guidance](https://learn.adafruit.com/adafruit-neopixel-uberguide/powering-neopixels).

## Firmware installation

Install Arduino CLI, Arduino-ESP32 **3.3.11**, and FastLED **3.10.5**:

```bash
brew install arduino-cli
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.11
arduino-cli lib install FastLED@3.10.5
```

The live sketch routes the upper-right (IO9) and upper-center (IO10) panels
through RMT while the other seven use the parallel LCD_CAM driver. On the
repeatable first minute of the Wex clip, the earlier all-LCD_CAM setup produced
isolated bright panel flashes in C920 photos. Routing both outputs through RMT
retained 24 FPS and produced no such flash in two 120-photo, one-minute
captures plus one 300-photo, one-minute capture at 5 FPS. The preloaded-video
firmware has a separate LCD_CAM implementation.
Firmware upload and serial acknowledgements do not verify signal quality at
the panel's DIN connector.

In Arduino IDE, select **ESP32S3 Dev Module**, **USB CDC On Boot → Disabled**,
and **Upload Speed → 115200**. The previous upload lost communication at
921600 baud; 115200 succeeded. Upload speed and the firmware's 2,000,000-baud
streaming speed are separate settings.

To compile without uploading:

```bash
arduino-cli compile \
  --fqbn 'esp32:esp32:esp32s3:CDCOnBoot=default,UploadSpeed=115200' \
  cs2_16x16_player_firmware
```

With the existing USB cable connected, upload and start the audio wave:

```bash
./start.sh --port /dev/cu.usbserial-1420 --style wave
```

Use the port reported by `python3 stream_arduino.py --list-ports`, or omit
`--port` if only one USB controller is connected. Run only one streamer per
serial port at a time. `./start.sh --no-upload --style wave` restarts just the
stream once the firmware has been installed.

The launcher defaults to the CH340 board options, `--display-size 48` and
`--fps 12`. It uses Arduino CLI from PATH, `ARDUINO_CLI`, or the existing local
`.build/tools/arduino-cli`, and keeps its build cache in `.build/panel48`.

## Frame reception and timing

The legacy sketch directory name is retained so existing launchers still find
it. The firmware accepts protocol-v1 HELLO packets for **48×48 RGB565**,
followed by **4,608-byte**, little-endian, top-left row-major frames with CRC32.
Compressed frames are decompressed only after their packet CRC passes.
A 16×16 or 32×32 HELLO is rejected. The frame duration in HELLO remains advisory:
the host schedules frame transmission with `--fps`.

The RGB565 receive buffer holds one complete frame for CRC verification.
The firmware waits before changing display data, maps the image into nine
panel buffers, submits it through the assigned drivers, and waits for both to finish.
A frame ACK means **transmission completed**, so the host starts the next
frame after LED output is finished.
Retries with the same sequence number are acknowledged without redisplay.
CLEAR waits for transmission to finish, blanks every panel, waits for that
transfer, and then acknowledges; it also resets duplicate-frame tracking.

The UART receive queue holds two full packets. Truncated or bad-CRC packets do
not replace the display. All FastLED calls run on the Arduino loop task;
The UART hardware can queue incoming bytes while LED output completes.

At 24 FPS, uncompressed RGB565 payloads require 110,592 bytes/second;
compression reduces the typical Wex clip well below that. Each LED output carries
256 pixels. Serial reception, LED transmission and rendering together
determine the sustainable frame rate.
The startup matrix test is disabled by default to avoid bright startup flashes.

Panel mapping and whole-screen flips live in
[`panel_layout.h`](../cs2_16x16_player_firmware/panel_layout.h).
Audio wave, audio spectrum, and video render at native 48×48 resolution.
Audio palettes, trails, slowed curves, and the browser preview follow the
selected display size. Lava retains its 16×16 artwork and scales it across the screen.
