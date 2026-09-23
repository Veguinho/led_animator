"""Exercise the actual sketch with a serial fake and guarded transfer ownership."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FASTLED_STUB = r"""
#pragma once
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

constexpr int WS2812B = 0, GRB = 0;
struct CRGB {
  uint8_t r, g, b;
  CRGB(uint8_t r=0, uint8_t g=0, uint8_t b=0): r(r), g(g), b(b) {}
  static const CRGB Red, Green, Blue, White;
};
const CRGB CRGB::Red(255,0,0), CRGB::Green(0,255,0),
           CRGB::Blue(0,0,255), CRGB::White(255,255,255);
namespace fl {
enum class Bus { RMT, I2S, LCD_CLOCKLESS };
struct TIMING_WS2812_800KHZ {};
struct ChannelOptions { Bus mBus = Bus::RMT; };
template<typename T> struct span {
  T *data;
  size_t size;
  span(T *data, size_t size): data(data), size(size) {}
};
template<typename TIMING> int makeClockless(int pin) { return pin; }
struct ChannelConfig {
  int pin;
  span<CRGB> pixels;
  Bus bus;
  ChannelConfig(int pin, span<CRGB> pixels, int, ChannelOptions options):
      pin(pin), pixels(pixels), bus(options.mBus) {}
};
}
struct FakeLED {
  struct Lane { int pin; CRGB *data; int length; fl::Bus bus; std::vector<CRGB> snapshot; };
  std::vector<Lane> lanes;
  bool inFlight = false;
  int shows = 0, brightness = 255, dither = 1;
  template<int CHIPSET, int PIN, int ORDER> void addLeds(CRGB *data, int count) {
    lanes.push_back({PIN, data, count, fl::Bus::RMT, {}});
  }
  template<fl::Bus BUS> void setExclusiveDriver() {}
  void add(const fl::ChannelConfig &config) {
    lanes.push_back({config.pin, config.pixels.data, int(config.pixels.size), config.bus, {}});
  }
  void setBrightness(int value) { brightness = value; }
  void setDither(int value) { dither = value; }
  void setMaxPowerInVoltsAndMilliamps(int, uint32_t) {}
  void wait() {
    if (inFlight) {
      for (auto &lane : lanes)
        assert(memcmp(lane.data, lane.snapshot.data(), lane.length*sizeof(CRGB)) == 0);
    }
    inFlight = false;
  }
  void show() {
    assert(!inFlight);
    assert(brightness == 255 && dither == 0);
    for (auto &lane : lanes) for (int i=0; i<lane.length; ++i) {
      const auto &color = lane.data[i];
      assert(int(color.r) + color.g + color.b <= 255);
    }
    for (auto &lane : lanes)
      lane.snapshot.assign(lane.data, lane.data + lane.length);
    inFlight = true;
    ++shows;
  }
  void clear(bool transmit) {
    assert(!inFlight);
    for (auto &lane : lanes) std::fill_n(lane.data, lane.length, CRGB());
    if (transmit) show();
  }
} FastLED;

struct FakeSerial {
  std::vector<uint8_t> incoming, outgoing;
  size_t cursor = 0, rxSize = 0;
  bool receivedDuringDMA = false;
  int showsAtResponse = -1;
  void setRxBufferSize(size_t size) { rxSize = size; }
  void begin(uint32_t baud) { assert(baud == 2000000); }
  void setTimeout(int) {}
  int available() { return incoming.size() - cursor; }
  int read() { return incoming[cursor++]; }
  size_t readBytes(char *destination, size_t length) {
    receivedDuringDMA |= FastLED.inFlight && length > 0;
    size_t count = std::min(length, incoming.size() - cursor);
    memcpy(destination, incoming.data() + cursor, count);
    cursor += count;
    return count;
  }
  void write(const uint8_t *data, size_t size) {
    if (outgoing.empty()) showsAtResponse = FastLED.shows;
    outgoing.insert(outgoing.end(), data, data + size);
  }
} Serial0;
FakeSerial Serial;  // A different endpoint: tests must exercise UART0.
void delay(int) {}
"""

HARNESS = r"""
#include "cs2_16x16_player_firmware.ino"

void request(uint8_t type, uint32_t sequence, std::vector<uint8_t> payload,
             bool corrupt = false, bool truncate = false) {
  std::vector<uint8_t> packet(16 + payload.size());
  memcpy(packet.data(), REQUEST_MAGIC, 4);
  packet[4] = PROTOCOL_VERSION;
  packet[5] = type;
  writeU16(packet.data()+6, payload.size());
  writeU32(packet.data()+8, sequence);
  writeU32(packet.data()+12, payloadCrc32(payload.data(), payload.size()) ^ corrupt);
  std::copy(payload.begin(), payload.end(), packet.begin()+16);
  if (truncate) packet.pop_back();
  Serial0.incoming = packet;
  Serial0.cursor = 0;
  Serial0.outgoing.clear();
  Serial0.showsAtResponse = -1;
  loop();
}
void response(uint8_t status, uint16_t detail, uint32_t sequence) {
  assert(Serial0.outgoing.size() == 12);
  assert(memcmp(Serial0.outgoing.data(), "LEDR", 4) == 0);
  assert(Serial0.outgoing[5] == status);
  assert(readU16(Serial0.outgoing.data()+6) == detail);
  assert(readU32(Serial0.outgoing.data()+8) == sequence);
}
int main() {
  // Every screen pixel maps to exactly one physical LED, including tile seams.
  bool seen[NUM_LEDS] = {};
  for (int row=0; row<48; ++row) for (int col=0; col<48; ++col) {
    const auto index = physicalIndex(row, col);
    assert(index < NUM_LEDS && !seen[index]);
    seen[index] = true;
  }
  assert(WIDTH == 48 && HEIGHT == 48 && NUM_LEDS == 2304);
  // Each GPIO owns one 16x16 tile in row-major screen order. Every tile gets
  // the same clockwise rotation and horizontal mirror before its serpentine
  // LED lookup.
  assert(physicalIndex(0,0) == 0 && physicalIndex(0,16) == 256);
  assert(physicalIndex(0,32) == 512);
  assert(physicalIndex(16,0) == 768 && physicalIndex(16,16) == 1024);
  assert(physicalIndex(16,32) == 1280);
  assert(physicalIndex(32,0) == 1536 && physicalIndex(32,16) == 1792);
  assert(physicalIndex(32,32) == 2048);
  assert(physicalIndex(1,0) == 1 && physicalIndex(1,16) == 257);
  assert(physicalIndex(0,15) == 255 && physicalIndex(16,15) == 1023);
  assert(physicalIndex(1,15) == 254 && physicalIndex(17,15) == 1022);
  assert(physicalIndex(17,0) == 769 && physicalIndex(17,16) == 1025);
  assert(physicalIndex(0,47) == 767 && physicalIndex(16,47) == 1535);
  assert(physicalIndex(47,47) == 2288);
  assert(payloadCrc32(reinterpret_cast<const uint8_t *>("123456789"),9) == 0xcbf43926);

  // Every possible incoming RGB565 color is attenuated, never boosted.
  for (uint32_t value=0; value<65536; ++value) {
    CRGB decoded = decodeRgb565(value), limited = limitPixelBrightness(decoded);
    assert(int(limited.r) + limited.g + limited.b <= MAX_TRANSMITTED_RGB_TOTAL);
    assert(limited.r <= decoded.r && limited.g <= decoded.g && limited.b <= decoded.b);
  }
  setup();
  assert(Serial0.rxSize >= 2*(4608+16));
  assert(FastLED.lanes.size() == 9);
  const int expectedPins[] = {11, 10, 9, 13, 12, 20, 46, 17, 18};
  for (int panel=0; panel<9; ++panel) {
    assert(FastLED.lanes[panel].pin == expectedPins[panel]);
    assert(FastLED.lanes[panel].data == leds+panel*256);
    assert(FastLED.lanes[panel].length == 256);
    assert(FastLED.lanes[panel].bus == fl::Bus::RMT);
  }
  std::vector<uint8_t> frame(4608, 0);
  request(PACKET_FRAME, 1, frame);
  response(STATUS_NAK, ERROR_NOT_READY, 1);
  request(PACKET_HELLO, 0, {16,16,1,0,0,0,0,0});
  response(STATUS_NAK, ERROR_BAD_DISPLAY, 0);
  request(PACKET_HELLO, 0, {32,32,1,0,0,0,0,0});
  response(STATUS_NAK, ERROR_BAD_DISPLAY, 0);
  request(PACKET_HELLO, 0, {48,48,1,0,0,0,0,0});
  response(STATUS_READY, 0, 0);

  // Different colors across a panel boundary. ACK is immediate so reception
  // of the next packet can overlap this hardware-timed transfer.
  writeU16(frame.data()+15*2, 0xf800);
  writeU16(frame.data()+16*2, 0x07e0);
  writeU16(frame.data()+(16*48)*2, 0x001f);
  writeU16(frame.data()+(16*48+16)*2, 0xffff);
  const int showsBeforeFrame = FastLED.shows;
  request(PACKET_FRAME, 1, frame);
  response(STATUS_ACK, 0, 1);
  assert(Serial0.showsAtResponse == showsBeforeFrame);
  assert(FastLED.shows == showsBeforeFrame + 1);
  assert(FastLED.inFlight);
  assert(leds[255].r == 255 && leds[256].g == 255);
  assert(leds[768].b == 255);
  assert(leds[1024].r == 85 && leds[1024].g == 85 && leds[1024].b == 85);
  const int shows = FastLED.shows;
  request(PACKET_FRAME, 1, frame);
  response(STATUS_ACK, 0, 1);
  assert(FastLED.inFlight);
  assert(FastLED.shows == shows);

  request(PACKET_FRAME, 2, frame, true);
  response(STATUS_NAK, ERROR_BAD_CRC, 2);
  request(PACKET_FRAME, 2, frame, false, true);
  response(STATUS_NAK, ERROR_BAD_LENGTH, 2);
  assert(FastLED.shows == shows);

  Serial0.receivedDuringDMA = false;
  writeU16(frame.data()+15*2, 0x001f);
  request(PACKET_FRAME, 2, frame);
  response(STATUS_ACK, 0, 2);
  assert(Serial0.receivedDuringDMA && FastLED.inFlight);
  assert(leds[255].b == 255 && leds[255].r == 0);

  request(PACKET_CLEAR, 99, {});
  response(STATUS_ACK, 0, 99);
  assert(!FastLED.inFlight);
  for (auto &led : leds) assert(led.r == 0 && led.g == 0 && led.b == 0);
  request(PACKET_FRAME, 2, frame);  // Same sequence can redraw after CLEAR.
  response(STATUS_ACK, 0, 2);
  assert(FastLED.inFlight && leds[255].b == 255);
  // A full-white packet cannot bypass limits, even if global settings changed.
  FastLED.setBrightness(255);
  FastLED.setDither(1);
  request(PACKET_FRAME, 3, std::vector<uint8_t>(4608, 255));
  response(STATUS_ACK, 0, 3);
  for (const auto &color : leds) assert(color.r == 85 && color.g == 85 && color.b == 85);
  showMatrixCoverageTest();  // The startup diagnostic uses the same limiter.
  FastLED.wait();
}
"""


class PanelFirmwareTests(unittest.TestCase):
    def test_audio_and_video_use_the_same_panel_layout(self):
        self.assertEqual(
            (ROOT / "cs2_16x16_player_firmware/panel_layout.h").read_text(),
            (ROOT / "preloaded_video/preloaded_video_firmware/panel_layout.h").read_text(),
        )

    @unittest.skipUnless(shutil.which("c++"), "a C++ compiler is required")
    def test_mapping_protocol_and_pipelined_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "FastLED.h").write_text(FASTLED_STUB)
            bus_traits = temp / "platforms/esp/32/drivers/rmt/rmt_5/bus_traits.h"
            bus_traits.parent.mkdir(parents=True)
            bus_traits.write_text("#pragma once\n")
            (temp / "test.cpp").write_text(HARNESS)
            subprocess.run([
                "c++", "-std=c++17", "-DCONFIG_IDF_TARGET_ESP32S3=1",
                "-DARDUINO_USB_CDC_ON_BOOT=0", "-DARDUINO_USB_MODE=1",
                "-I", str(temp), "-I", str(ROOT / "cs2_16x16_player_firmware"),
                str(temp / "test.cpp"), "-o", str(temp / "test"),
            ], check=True, capture_output=True, text=True)
            subprocess.run([str(temp / "test")], check=True, capture_output=True)
