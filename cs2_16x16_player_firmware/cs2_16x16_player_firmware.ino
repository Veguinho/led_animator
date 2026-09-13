#include <FastLED.h>
#include "panel_layout.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "This sketch requires an ESP32-S3."
#endif

constexpr uint8_t BRIGHTNESS = 255;

// Total estimated LED budget across ALL four panels. Keep the existing limit
// until the external 5 V supply, fuses and power wiring have been sized.
// This software estimate is not a substitute for hardware current protection.
constexpr uint32_t MAX_POWER_MILLIAMPS = 2000;
constexpr bool RUN_STARTUP_MATRIX_TEST = false;

// The installed board's only USB socket uses a CH340 connected to UART0.
// Match the live Python app; firmware flashing remains at 115200 baud.
constexpr uint32_t SERIAL_BAUD = 2000000;
constexpr uint16_t BYTES_PER_FRAME = NUM_LEDS * 2;
constexpr uint16_t MAX_PACKET_PAYLOAD = BYTES_PER_FRAME;

// Always address UART0 explicitly, even if a build enables native USB CDC.
#define PANEL_SERIAL Serial0

constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t PACKET_HELLO = 1;
constexpr uint8_t PACKET_FRAME = 2;
constexpr uint8_t PACKET_CLEAR = 3;
constexpr uint8_t STATUS_READY = 1;
constexpr uint8_t STATUS_ACK = 2;
constexpr uint8_t STATUS_NAK = 3;

constexpr uint16_t ERROR_BAD_VERSION = 1;
constexpr uint16_t ERROR_BAD_LENGTH = 2;
constexpr uint16_t ERROR_BAD_CRC = 3;
constexpr uint16_t ERROR_BAD_TYPE = 4;
constexpr uint16_t ERROR_NOT_READY = 5;
constexpr uint16_t ERROR_BAD_DISPLAY = 6;

const uint8_t REQUEST_MAGIC[4] = {'L', 'E', 'D', 'S'};
const uint8_t RESPONSE_MAGIC[4] = {'L', 'E', 'D', 'R'};

CRGB leds[NUM_LEDS];
// Only a complete, CRC-verified payload can replace the display buffer.
uint8_t packetPayload[MAX_PACKET_PAYLOAD];
bool streamReady = false;
bool hasLastFrame = false;
uint32_t lastFrameSequence = 0;

uint16_t readU16(const uint8_t *bytes) {
  return static_cast<uint16_t>(bytes[0]) |
         (static_cast<uint16_t>(bytes[1]) << 8);
}

uint32_t readU32(const uint8_t *bytes) {
  return static_cast<uint32_t>(bytes[0]) |
         (static_cast<uint32_t>(bytes[1]) << 8) |
         (static_cast<uint32_t>(bytes[2]) << 16) |
         (static_cast<uint32_t>(bytes[3]) << 24);
}

void writeU16(uint8_t *bytes, uint16_t value) {
  bytes[0] = value & 0xff;
  bytes[1] = value >> 8;
}

void writeU32(uint8_t *bytes, uint32_t value) {
  bytes[0] = value & 0xff;
  bytes[1] = (value >> 8) & 0xff;
  bytes[2] = (value >> 16) & 0xff;
  bytes[3] = (value >> 24) & 0xff;
}

uint32_t payloadCrc32(const uint8_t *bytes, uint16_t length) {
  uint32_t crc = 0xffffffffUL;
  for (uint16_t index = 0; index < length; ++index) {
    crc ^= bytes[index];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      const uint32_t mask = -(crc & 1UL);
      crc = (crc >> 1) ^ (0xedb88320UL & mask);
    }
  }
  return crc ^ 0xffffffffUL;
}

CRGB decodeRgb565(uint16_t color) {
  const uint8_t red5 = (color >> 11) & 0x1f;
  const uint8_t green6 = (color >> 5) & 0x3f;
  const uint8_t blue5 = color & 0x1f;
  return CRGB(
      (red5 << 3) | (red5 >> 2),
      (green6 << 2) | (green6 >> 4),
      (blue5 << 3) | (blue5 >> 2));
}

void showMatrixCoverageTest() {
  FastLED.wait();
  for (uint8_t row = 0; row < HEIGHT; ++row) {
    for (uint8_t column = 0; column < WIDTH; ++column) {
      const CRGB color = (row & 1U) ? CRGB(0, 0, 24) : CRGB(0, 24, 0);
      leds[physicalIndex(row, column)] = color;
    }
  }
  leds[physicalIndex(0, 0)] = CRGB::Red;
  leds[physicalIndex(0, WIDTH - 1)] = CRGB::Green;
  leds[physicalIndex(HEIGHT - 1, 0)] = CRGB::Blue;
  leds[physicalIndex(HEIGHT - 1, WIDTH - 1)] = CRGB::White;
  FastLED.show();
  delay(1500);
  FastLED.wait();
  FastLED.clear(true);
}

void sendResponse(uint8_t status, uint16_t detail, uint32_t sequence) {
  uint8_t response[12];
  memcpy(response, RESPONSE_MAGIC, sizeof(RESPONSE_MAGIC));
  response[4] = PROTOCOL_VERSION;
  response[5] = status;
  writeU16(response + 6, detail);
  writeU32(response + 8, sequence);
  PANEL_SERIAL.write(response, sizeof(response));
}

bool readExact(uint8_t *destination, uint16_t length) {
  return PANEL_SERIAL.readBytes(reinterpret_cast<char *>(destination), length) ==
         length;
}

bool findRequestMagic() {
  static uint8_t matched = 0;
  while (PANEL_SERIAL.available() > 0) {
    const uint8_t value = PANEL_SERIAL.read();
    if (value == REQUEST_MAGIC[matched]) {
      ++matched;
      if (matched == sizeof(REQUEST_MAGIC)) {
        matched = 0;
        return true;
      }
    } else {
      matched = value == REQUEST_MAGIC[0] ? 1 : 0;
    }
  }
  return false;
}

void drawFrame(const uint8_t *payload) {
  // Never change LED data still owned by an in-flight transmission.
  FastLED.wait();
  for (uint16_t logicalIndex = 0; logicalIndex < NUM_LEDS; ++logicalIndex) {
    const uint16_t offset = logicalIndex * 2;
    const uint16_t color = readU16(payload + offset);
    const uint8_t row = logicalIndex / WIDTH;
    const uint8_t column = logicalIndex % WIDTH;
    leds[physicalIndex(row, column)] = decodeRgb565(color);
  }
  FastLED.show();
  // Finish the LED transfer before ACK lets the host send another frame.
  FastLED.wait();
}

void handlePacket(uint8_t type, uint16_t length, uint32_t sequence) {
  if (type == PACKET_HELLO) {
    // width, height, pixel format, reserved, frame duration (microseconds)
    if (length != 8 || packetPayload[0] != WIDTH ||
        packetPayload[1] != HEIGHT || packetPayload[2] != 1) {
      sendResponse(STATUS_NAK, ERROR_BAD_DISPLAY, sequence);
      return;
    }
    streamReady = true;
    hasLastFrame = false;
    sendResponse(STATUS_READY, 0, sequence);
    return;
  }

  if (type == PACKET_FRAME) {
    if (!streamReady) {
      sendResponse(STATUS_NAK, ERROR_NOT_READY, sequence);
      return;
    }
    if (length != BYTES_PER_FRAME) {
      sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
      return;
    }
    // If an ACK was lost, acknowledge the retry without drawing it twice.
    if (!hasLastFrame || sequence != lastFrameSequence) {
      drawFrame(packetPayload);
      lastFrameSequence = sequence;
      hasLastFrame = true;
    }
    sendResponse(STATUS_ACK, 0, sequence);
    return;
  }

  if (type == PACKET_CLEAR) {
    if (length != 0) {
      sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
      return;
    }
    FastLED.wait();
    FastLED.clear(true);
    FastLED.wait();  // CLEAR is complete when acknowledged.
    hasLastFrame = false;
    sendResponse(STATUS_ACK, 0, sequence);
    return;
  }

  sendResponse(STATUS_NAK, ERROR_BAD_TYPE, sequence);
}

void receivePacket() {
  if (!findRequestMagic()) {
    return;
  }

  uint8_t header[12];
  if (!readExact(header, sizeof(header))) {
    return;
  }

  const uint8_t version = header[0];
  const uint8_t type = header[1];
  const uint16_t length = readU16(header + 2);
  const uint32_t sequence = readU32(header + 4);
  const uint32_t expectedCrc = readU32(header + 8);

  if (version != PROTOCOL_VERSION) {
    sendResponse(STATUS_NAK, ERROR_BAD_VERSION, sequence);
    return;
  }
  if (length > MAX_PACKET_PAYLOAD) {
    sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
    return;
  }
  if (!readExact(packetPayload, length)) {
    sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
    return;
  }
  if (payloadCrc32(packetPayload, length) != expectedCrc) {
    sendResponse(STATUS_NAK, ERROR_BAD_CRC, sequence);
    return;
  }
  handlePacket(type, length, sequence);
}

void setup() {
  PANEL_SERIAL.setRxBufferSize(2 * (BYTES_PER_FRAME + 16));
  PANEL_SERIAL.begin(SERIAL_BAUD);
  // Allow margin for USB packet gaps and incomplete frames.
  PANEL_SERIAL.setTimeout(500);

  // Use the same RMT-backed controllers as the working single-panel sketch.
  static_assert(PANEL_COUNT == 4, "This firmware configures four RMT outputs.");
  FastLED.addLeds<WS2812B, DATA_PINS[0], GRB>(leds, PANEL_LEDS);
  FastLED.addLeds<WS2812B, DATA_PINS[1], GRB>(leds + PANEL_LEDS, PANEL_LEDS);
  FastLED.addLeds<WS2812B, DATA_PINS[2], GRB>(leds + 2 * PANEL_LEDS, PANEL_LEDS);
  FastLED.addLeds<WS2812B, DATA_PINS[3], GRB>(leds + 3 * PANEL_LEDS, PANEL_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, MAX_POWER_MILLIAMPS);
  FastLED.clear(true);
  FastLED.wait();

  if (RUN_STARTUP_MATRIX_TEST) {
    showMatrixCoverageTest();
  }
}

void loop() {
  receivePacket();
  if (PANEL_SERIAL.available() == 0) {
    delay(1);  // Yield while waiting for the next frame.
  }
}
