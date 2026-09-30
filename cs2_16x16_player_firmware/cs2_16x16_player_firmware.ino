#include <FastLED.h>
#include <miniz.h>
#include "platforms/esp/32/drivers/lcd_spi/bus_traits.h"
#include "panel_layout.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "This sketch requires an ESP32-S3."
#endif

// Bake a 60/255 brightness ceiling into every pixel byte before it reaches
// any output lane. Keep the driver at 255 so all nine GPIOs transmit the same
// already-limited values, including the top-right panel on GPIO9.
constexpr uint8_t OUTPUT_SCALE = 60;
constexpr uint8_t DRIVER_BRIGHTNESS = 255;
constexpr uint16_t MAX_PIXEL_RGB_TOTAL = 64;
constexpr uint16_t MAX_TRANSMITTED_RGB_TOTAL =
    MAX_PIXEL_RGB_TOTAL * OUTPUT_SCALE / 255;
constexpr uint8_t MAX_VIDEO_RGB_TOTAL = 4;

// FastLED estimates 1 mA of idle draw per LED: nine 16x16 panels already
// exceed 2 A before any channel lights. A 4 A ceiling leaves room for that
// baseline plus the firmware's separately capped channel output.
// This software estimate is not a substitute for hardware current protection.
constexpr uint32_t MAX_POWER_MILLIAMPS = 4000;
constexpr bool RUN_STARTUP_MATRIX_TEST = false;

// The installed board's only USB socket uses a CH340 connected to UART0.
// Match the live Python app; firmware flashing remains at 115200 baud.
constexpr uint32_t SERIAL_BAUD = 1500000;
constexpr uint16_t BYTES_PER_FRAME = NUM_LEDS * 2;
constexpr uint16_t MAX_PACKET_PAYLOAD = BYTES_PER_FRAME;

// Always address UART0 explicitly, even if a build enables native USB CDC.
#define PANEL_SERIAL Serial0

constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t PACKET_HELLO = 1;
constexpr uint8_t PACKET_FRAME = 2;
constexpr uint8_t PACKET_CLEAR = 3;
constexpr uint8_t PACKET_COMPRESSED_FRAME = 4;
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
uint8_t expandedFrame[BYTES_PER_FRAME];
tinfl_decompressor frameDecompressor;
bool streamReady = false;
bool videoMode = false;
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

CRGB limitPixelBrightness(CRGB color) {
  const uint16_t total = static_cast<uint16_t>(color.r) + color.g + color.b;
  if (total > MAX_PIXEL_RGB_TOTAL) {
    color.r = static_cast<uint16_t>(color.r) * MAX_PIXEL_RGB_TOTAL / total;
    color.g = static_cast<uint16_t>(color.g) * MAX_PIXEL_RGB_TOTAL / total;
    color.b = static_cast<uint16_t>(color.b) * MAX_PIXEL_RGB_TOTAL / total;
  }
  color.r = static_cast<uint16_t>(color.r) * OUTPUT_SCALE / 255;
  color.g = static_cast<uint16_t>(color.g) * OUTPUT_SCALE / 255;
  color.b = static_cast<uint16_t>(color.b) * OUTPUT_SCALE / 255;
  return color;
}

void showLimitedFrame(bool videoFrame = false) {
  FastLED.wait();
  for (uint16_t index = 0; index < NUM_LEDS; ++index) {
    CRGB color = limitPixelBrightness(leds[index]);
    if (videoFrame) {
      const uint16_t total = static_cast<uint16_t>(color.r) + color.g + color.b;
      if (total > MAX_VIDEO_RGB_TOTAL) {
        color.r = static_cast<uint16_t>(color.r) * MAX_VIDEO_RGB_TOTAL / total;
        color.g = static_cast<uint16_t>(color.g) * MAX_VIDEO_RGB_TOTAL / total;
        color.b = static_cast<uint16_t>(color.b) * MAX_VIDEO_RGB_TOTAL / total;
      }
    }
    leds[index] = color;
  }
  // Reapply the ceilings at every transmission, including startup tests.
  FastLED.setBrightness(DRIVER_BRIGHTNESS);
  FastLED.setDither(0);
  FastLED.show();
  // Video ACK can follow queueing the transfer: the next packet is received
  // into separate storage, and drawFrame waits before changing leds again.
  // This overlaps UART reception with LED output at the source frame rate.
  if (!videoFrame) {
    FastLED.wait();
  }
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
  showLimitedFrame();
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
  // In video mode, the next packet may arrive during this LED transfer;
  // the wait at the top protects the buffer before the next frame replaces it.
  showLimitedFrame(videoMode);
}

void handlePacket(uint8_t type, uint16_t length, uint32_t sequence) {
  if (type == PACKET_HELLO) {
    // width, height, pixel format, reserved, frame duration (microseconds)
    if (length != 8 || packetPayload[0] != WIDTH ||
        packetPayload[1] != HEIGHT || packetPayload[2] != 1 ||
        packetPayload[3] > 1) {
      sendResponse(STATUS_NAK, ERROR_BAD_DISPLAY, sequence);
      return;
    }
    streamReady = true;
    videoMode = packetPayload[3] == 1;
    hasLastFrame = false;
    sendResponse(STATUS_READY, 0, sequence);
    return;
  }

  if (type == PACKET_FRAME || type == PACKET_COMPRESSED_FRAME) {
    if (!streamReady) {
      sendResponse(STATUS_NAK, ERROR_NOT_READY, sequence);
      return;
    }
    if ((type == PACKET_FRAME && length != BYTES_PER_FRAME) ||
        (type == PACKET_COMPRESSED_FRAME && (length == 0 || length >= BYTES_PER_FRAME))) {
      sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
      return;
    }
    // If an ACK was lost, acknowledge the retry without drawing it twice.
    if (!hasLastFrame || sequence != lastFrameSequence) {
      const uint8_t *frame = packetPayload;
      if (type == PACKET_COMPRESSED_FRAME) {
        tinfl_init(&frameDecompressor);
        size_t inputLength = length;
        size_t outputLength = sizeof(expandedFrame);
        const tinfl_status result = tinfl_decompress(
            &frameDecompressor, packetPayload, &inputLength,
            expandedFrame, expandedFrame, &outputLength,
            TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
        if (result != TINFL_STATUS_DONE || inputLength != length ||
            outputLength != BYTES_PER_FRAME) {
          sendResponse(STATUS_NAK, ERROR_BAD_LENGTH, sequence);
          return;
        }
        frame = expandedFrame;
      }
      drawFrame(frame);
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

  // IO9 and IO10 use RMT to avoid flashes seen on those two tiles; the
  // other seven use ESP32-S3 parallel LCD_CAM. Runtime channel configuration
  // is also required because GPIO20 is valid for this
  // CH340/UART board but intentionally blocked by FastLED's compile-time API
  // as the native USB D+ pin.
  static_assert(PANEL_COUNT == 9, "This firmware configures nine panel outputs.");
  for (uint8_t panel = 0; panel < PANEL_COUNT; ++panel) {
    fl::ChannelOptions options;
    options.mBus = (DATA_PINS[panel] == 10 || DATA_PINS[panel] == 9)
        ? fl::Bus::RMT : fl::Bus::LCD_CLOCKLESS;
    FastLED.add(fl::ChannelConfig(
        fl::makeClockless<fl::TIMING_WS2812_800KHZ>(DATA_PINS[panel]),
        fl::span<CRGB>(leds + panel * PANEL_LEDS, PANEL_LEDS), GRB, options));
  }
  FastLED.setBrightness(DRIVER_BRIGHTNESS);
  FastLED.setDither(0);
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
