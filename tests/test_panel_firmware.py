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
struct FakeLED {
  struct Lane { int pin; CRGB *data; int length; std::vector<CRGB> snapshot; };
  std::vector<Lane> lanes;
  bool inFlight = false;
  int shows = 0;
  template<int CHIPSET, int PIN, int ORDER> void addLeds(CRGB *data, int count) {
    lanes.push_back({PIN, data, count, {}});
  }
  void setBrightness(int) {}
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
  loop();
}
void response(uint8_t status, uint16_t detail, uint32_t sequence) {
  assert(!FastLED.inFlight);  // Every protocol response follows a completed transfer.
  assert(Serial0.outgoing.size() == 12);
  assert(memcmp(Serial0.outgoing.data(), "LEDR", 4) == 0);
  assert(Serial0.outgoing[5] == status);
  assert(readU16(Serial0.outgoing.data()+6) == detail);
  assert(readU32(Serial0.outgoing.data()+8) == sequence);
}
int main() {
  // Every screen pixel maps to exactly one physical LED, including tile seams.
  bool seen[NUM_LEDS] = {};
  for (int row=0; row<32; ++row) for (int col=0; col<32; ++col) {
    const auto index = physicalIndex(row, col);
    assert(index < NUM_LEDS && !seen[index]);
    seen[index] = true;
  }
  assert(WIDTH == 32 && HEIGHT == 32 && NUM_LEDS == 1024);
  assert(physicalIndex(0,0) == 0 && physicalIndex(0,16) == 256);
  assert(physicalIndex(16,0) == 512 && physicalIndex(16,16) == 768);
  assert(physicalIndex(1,0) == 31 && physicalIndex(1,16) == 287);
  assert(physicalIndex(31,31) == 1008);
  assert(payloadCrc32(reinterpret_cast<const uint8_t *>("123456789"),9) == 0xcbf43926);

  setup();
  assert(Serial0.rxSize >= 2*(2048+16));
  assert(FastLED.lanes.size() == 4);
  for (int panel=0; panel<4; ++panel) {
    assert(FastLED.lanes[panel].pin == 10+panel);
    assert(FastLED.lanes[panel].data == leds+panel*256);
    assert(FastLED.lanes[panel].length == 256);
  }
  std::vector<uint8_t> frame(2048, 0);
  request(PACKET_FRAME, 1, frame);
  response(STATUS_NAK, ERROR_NOT_READY, 1);
  request(PACKET_HELLO, 0, {16,16,1,0,0,0,0,0});
  response(STATUS_NAK, ERROR_BAD_DISPLAY, 0);
  request(PACKET_HELLO, 0, {48,48,1,0,0,0,0,0});
  response(STATUS_NAK, ERROR_BAD_DISPLAY, 0);
  request(PACKET_HELLO, 0, {32,32,1,0,0,0,0,0});
  response(STATUS_READY, 0, 0);

  // Different colors across a panel boundary; the transfer must finish before ACK.
  writeU16(frame.data()+15*2, 0xf800);
  writeU16(frame.data()+16*2, 0x07e0);
  writeU16(frame.data()+(16*32)*2, 0x001f);
  writeU16(frame.data()+(16*32+16)*2, 0xffff);
  request(PACKET_FRAME, 1, frame);
  response(STATUS_ACK, 0, 1);
  assert(!FastLED.inFlight);
  assert(leds[15].r == 255 && leds[256].g == 255);
  assert(leds[512].b == 255);
  assert(leds[768].r == 255 && leds[768].g == 255 && leds[768].b == 255);
  const int shows = FastLED.shows;
  request(PACKET_FRAME, 1, frame);
  response(STATUS_ACK, 0, 1);
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
  assert(!Serial0.receivedDuringDMA && !FastLED.inFlight);
  assert(leds[15].b == 255 && leds[15].r == 0);

  request(PACKET_CLEAR, 99, {});
  response(STATUS_ACK, 0, 99);
  assert(!FastLED.inFlight);
  for (auto &led : leds) assert(led.r == 0 && led.g == 0 && led.b == 0);
  request(PACKET_FRAME, 2, frame);  // Same sequence can redraw after CLEAR.
  response(STATUS_ACK, 0, 2);
  assert(!FastLED.inFlight && leds[15].b == 255);
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
    def test_mapping_protocol_and_completed_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            (temp / "FastLED.h").write_text(FASTLED_STUB)
            (temp / "test.cpp").write_text(HARNESS)
            subprocess.run([
                "c++", "-std=c++17", "-DCONFIG_IDF_TARGET_ESP32S3=1",
                "-DARDUINO_USB_CDC_ON_BOOT=0", "-DARDUINO_USB_MODE=1",
                "-I", str(temp), "-I", str(ROOT / "cs2_16x16_player_firmware"),
                str(temp / "test.cpp"), "-o", str(temp / "test"),
            ], check=True, capture_output=True, text=True)
            subprocess.run([str(temp / "test")], check=True, capture_output=True)
