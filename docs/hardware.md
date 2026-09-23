# Hardware setup

[← README](../README.md) · [Live streaming](live-streaming.md)

The streaming firmware drives a **48×48 screen** made from nine 16×16
WS2812B panels. The live ESP32-S3 firmware uses nine hardware-timed FastLED RMT outputs,
matching the controller API used by the single-panel sketch. The Mac sends frames over the controller's
**existing CH340 USB-to-UART** port at 2,000,000 baud; an external regulated
5 V supply powers the LEDs. Full-resolution streaming defaults to **30 FPS**.

## Panel placement and wiring

Viewed from the front, mount all panels with pixel 0 at the top-left and
horizontal serpentine wiring (the first row runs left to right):

| | Left | Center | Right |
| :-- | :-- | :-- | :-- |
| Top | IO11 → panel 1 DIN | IO10 → panel 2 DIN | IO09 → panel 3 DIN |
| Middle | IO13 → panel 4 DIN | IO12 → panel 5 DIN | IO20 → panel 6 DIN |
| Bottom | IO46 → panel 7 DIN | IO17 → panel 8 DIN | IO18 → panel 9 DIN |

The firmware applies the same clockwise rotation and horizontal mirror
correction to every panel.

Each arrow above goes through its own **3.3 V → 5 V level-shifter channel**
(e.g. three 74AHCT125 chips provide enough channels), followed by a 330–470 Ω
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
native USB connector, IO19/IO20 wiring, or board replacement is needed.
The 48×48 payload is 4608 bytes; with the packet header and UART framing,
each transfer takes about 23.1 ms on the wire at 2,000,000 baud. UART reception
can overlap hardware-timed LED output. The previous 60 FPS native USB
configuration is not used with this hardware.

The separate MP4-player firmware negotiates **2,000,000 baud** after booting
at 230400. Measured checksummed upload throughput is about 129 KB/s; see the
[buffered MP4 mode](../mp4_player/README.md). The live-audio firmware starts
directly at 2,000,000 baud.

## Power

Feed power directly from the external supply to each panel. Do not pass the
screen's power through the ESP32, USB, or one panel's small power connector.
Use wire sizes, connectors, fuses and power injection suitable for the actual
panel current and cable lengths. Keep the external +5 V off the USB-powered
ESP32's 5 V pin unless the specific board supports that arrangement.

Live frames and the startup diagnostic clamp each pixel to a total
`R + G + B` of **255**, preserving color ratios while allowing sparse video
highlights to use substantially more of the LEDs' dynamic range.
Temporal dithering is disabled, and both limits are reapplied before every
non-black transmission. Even a valid full-white host frame is limited. This
bounds the values sent by firmware; it cannot guarantee brightness if the
data signal is corrupted after leaving the controller.

The live firmware preserves source brightness up to **255/255** and caps the
estimated whole-panel output at **2 A total**
FastLED power budget across all nine panels. This deliberately low budget
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

The live sketch pins all nine runtime channels to FastLED's hardware-timed RMT
backend. Runtime channel configuration keeps GPIO20 available on this
CH340/UART board. Firmware upload and correct serial acknowledgements do not by
themselves verify electrical signal quality.

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
`--fps 30`. It uses Arduino CLI from PATH, `ARDUINO_CLI`, or the existing local
`.build/tools/arduino-cli`, and keeps its build cache in `.build/panel48`.

## Frame reception and timing

The legacy sketch directory name is retained so existing launchers still find
it. The firmware accepts protocol-v1 HELLO packets for **48×48 RGB565**,
followed by **4608-byte**, little-endian, top-left row-major frames with CRC32.
A 16×16 or 32×32 HELLO is rejected. The frame duration in HELLO remains advisory:
the host schedules frame transmission with `--fps`.

The RGB565 receive buffer holds one complete frame for CRC verification.
The firmware acknowledges a complete CRC-verified receive buffer, maps it into
nine panel buffers, and submits all lanes through RMT. The next
packet can arrive in the UART buffer while LED output continues. The next frame
waits for DMA before changing LED data.
Retries with the same sequence number are acknowledged without redisplay.
CLEAR waits for transmission to finish, blanks every panel, waits for that
transfer, and then acknowledges; it also resets duplicate-frame tracking.

The UART receive queue holds two full packets. Truncated or bad-CRC packets do
not replace the display. All FastLED calls run on the Arduino loop task;
The UART hardware can queue incoming bytes while LED output completes.

At 30 FPS, RGB565 payloads require 138,240 bytes/second. Each LED output carries
256 pixels. Serial reception, LED transmission and rendering together
determine the sustainable frame rate.
The startup matrix test is disabled by default to avoid bright startup flashes.

Panel mapping and whole-screen flips live in
[`panel_layout.h`](../cs2_16x16_player_firmware/panel_layout.h).
Audio wave, audio spectrum, and video render at native 48×48 resolution.
Audio palettes, trails, slowed curves, and the browser preview follow the
selected display size. Lava retains its 16×16 artwork and scales each pixel
to a 3×3 block across the new screen.
