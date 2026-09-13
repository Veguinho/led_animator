# Hardware setup

[← README](../README.md) · [Live streaming](live-streaming.md)

The streaming firmware drives a **32×32 screen** made from four 16×16
WS2812B panels. The live ESP32-S3 firmware uses four FastLED RMT outputs,
matching the controller API used by the single-panel sketch. The Mac sends frames over the controller's
**existing CH340 USB-to-UART** port at 2,000,000 baud; an external regulated
5 V supply powers the LEDs. Full-resolution streaming defaults to **20 FPS**.

## Panel placement and wiring

Viewed from the front, mount all panels with pixel 0 at the top-left and
horizontal serpentine wiring (the first row runs left to right):

| | Left | Right |
| :-- | :-- | :-- |
| Top | IO10 → panel 1 DIN | IO11 → panel 2 DIN |
| Bottom | IO12 → panel 3 DIN | IO13 → panel 4 DIN |

Each arrow above goes through its own **3.3 V → 5 V level-shifter channel**
(e.g. one 74AHCT125 chip provides four channels), followed by a 330–470 Ω
resistor close to that panel's DIN. Enable the used shifter outputs. Leave the
panels' DOUT connectors unconnected: each GPIO drives only its own panel.

| Connection | Wire it to |
| :-- | :-- |
| Mac USB | Existing USB socket wired through the CH340 to ESP32-S3 UART0 |
| IO10–IO13 | Four level-shifter inputs, as mapped above |
| External supply +5 V | Separate, suitably fused power branches to each panel and to the level shifters |
| External supply GND | Every panel GND, ESP32 GND, and level-shifter GND |
| Recommended local capacitor | 500–1000 µF across 5 V/GND at each panel's power input, with correct polarity |

The attached controller appears as `/dev/cu.usbserial-1420` on this Mac
(CH340 VID:PID `1a86:7523`). The firmware explicitly uses `Serial0`, so no
native USB connector, IO19/IO20 wiring, or board replacement is needed.
The 32×32 payload is 2048 bytes; with the packet header and UART framing,
each transfer takes about 10.3 ms on the wire at 2,000,000 baud. A 20 FPS default leaves room
for frame preparation and acknowledgements. The previous 60 FPS native USB
configuration is not used with this hardware.

The separate preloaded-video firmware negotiates **2,000,000 baud** after
booting at 230400. Measured checksummed upload throughput is about 129 KB/s;
see [video mode](../preloaded_video/README.md). The live-audio firmware starts directly at 2,000,000 baud.

## Power

Feed power directly from the external supply to each panel. Do not pass the
screen's power through the ESP32, USB, or one panel's small power connector.
Use wire sizes, connectors, fuses and power injection suitable for the actual
panel current and cable lengths. Keep the external +5 V off the USB-powered
ESP32's 5 V pin unless the specific board supports that arrangement.

The live firmware uses maximum brightness **255/255** and an estimated **2 A total**
FastLED power budget across all four panels. This deliberately low budget
will dim large bright areas. It is a software estimate, not a hardware current
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

The live sketch uses four standard `FastLED.addLeds<WS2812B, pin, GRB>`
controllers, bound to RMT by the installed FastLED version. This replaces
LCD_CAM to address bright flashes during live playback; the four-panel setup
was confirmed working after this change. The preloaded-video firmware
still uses the separate LCD_CAM Channels API. Firmware upload and correct
serial acknowledgements do not by themselves verify electrical signal quality.

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

The launcher defaults to the CH340 board options, `--display-size 32` and
`--fps 20`. It uses Arduino CLI from PATH, `ARDUINO_CLI`, or the existing local
`.build/tools/arduino-cli`, and keeps its build cache in `.build/panel32`.

## Frame reception and timing

The legacy sketch directory name is retained so existing launchers still find
it. The firmware accepts protocol-v1 HELLO packets for **32×32 RGB565**,
followed by **2048-byte**, little-endian, top-left row-major frames with CRC32.
A 16×16 or 48×48 HELLO is rejected. The frame duration in HELLO remains advisory:
the host schedules frame transmission with `--fps`.

The RGB565 receive buffer holds one complete frame for CRC verification.
The firmware waits before changing display data, maps the image into four
panel buffers, submits it through RMT, and waits for that transfer to finish.
A frame ACK means **transmission completed**, so the host starts the next
frame after LED output is finished.
Retries with the same sequence number are acknowledged without redisplay.
CLEAR waits for transmission to finish, blanks every panel, waits for that
transfer, and then acknowledges; it also resets duplicate-frame tracking.

The UART receive queue holds two full packets. Truncated or bad-CRC packets do
not replace the display. All FastLED calls run on the Arduino loop task;
The UART hardware can queue incoming bytes while LED output completes.

At 20 FPS, RGB565 payloads require 40,960 bytes/second. Each LED output carries
256 pixels. Serial reception, LED transmission and rendering together
determine the sustainable frame rate.
The startup matrix test is disabled by default to avoid bright startup flashes.

Panel mapping and whole-screen flips live in
[`panel_layout.h`](../cs2_16x16_player_firmware/panel_layout.h).
Audio wave, audio spectrum, and video render at native 32×32 resolution.
Audio palettes, trails, slowed curves, and the browser preview follow the
selected display size. Lava retains its 16×16 artwork and scales each pixel
to a 2×2 block across the new screen.
