#include <FastLED.h>

constexpr uint8_t DATA_PIN = 10;
constexpr uint8_t WIDTH = 16;
constexpr uint8_t HEIGHT = 16;
constexpr uint16_t NUM_LEDS = WIDTH * HEIGHT;
constexpr uint8_t BRIGHTNESS = 48;

// A matriz e formada por uma unica cadeia em zigue-zague. Estas opcoes mudam
// somente a orientacao da imagem; nenhuma delas reduz a area desenhada.
constexpr bool SERPENTINE_LAYOUT = true;
constexpr bool FLIP_HORIZONTAL = false;
constexpr bool FLIP_VERTICAL = false;

// Use uma fonte 5 V externa de pelo menos 2 A e una o GND da fonte ao GND do
// ESP32. Nao alimente a matriz inteira pelo pino 5 V/USB.
constexpr uint16_t MAX_POWER_MILLIAMPS = 2000;
constexpr bool RUN_STARTUP_MATRIX_TEST = true;

constexpr uint32_t SERIAL_BAUD = 921600;
constexpr uint16_t BYTES_PER_FRAME = NUM_LEDS * 2;
constexpr uint16_t MAX_PACKET_PAYLOAD = BYTES_PER_FRAME;

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

uint16_t physicalIndex(uint8_t row, uint8_t column) {
  if (FLIP_VERTICAL) {
    row = HEIGHT - 1 - row;
  }
  if (FLIP_HORIZONTAL) {
    column = WIDTH - 1 - column;
  }
  if (SERPENTINE_LAYOUT && (row & 1U)) {
    column = WIDTH - 1 - column;
  }
  return static_cast<uint16_t>(row) * WIDTH + column;
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
  FastLED.clear(true);
}

void sendResponse(uint8_t status, uint16_t detail, uint32_t sequence) {
  uint8_t response[12];
  memcpy(response, RESPONSE_MAGIC, sizeof(RESPONSE_MAGIC));
  response[4] = PROTOCOL_VERSION;
  response[5] = status;
  writeU16(response + 6, detail);
  writeU32(response + 8, sequence);
  Serial.write(response, sizeof(response));
}

bool readExact(uint8_t *destination, uint16_t length) {
  return Serial.readBytes(reinterpret_cast<char *>(destination), length) == length;
}

bool findRequestMagic() {
  static uint8_t matched = 0;
  while (Serial.available() > 0) {
    const uint8_t value = Serial.read();
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
  for (uint16_t logicalIndex = 0; logicalIndex < NUM_LEDS; ++logicalIndex) {
    const uint16_t offset = logicalIndex * 2;
    const uint16_t color = readU16(payload + offset);
    const uint8_t row = logicalIndex / WIDTH;
    const uint8_t column = logicalIndex % WIDTH;
    leds[physicalIndex(row, column)] = decodeRgb565(color);
  }
  FastLED.show();
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
    FastLED.clear(true);
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
  Serial.begin(SERIAL_BAUD);
  Serial.setTimeout(100);

  FastLED.addLeds<WS2812B, DATA_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, MAX_POWER_MILLIAMPS);
  FastLED.clear(true);

  if (RUN_STARTUP_MATRIX_TEST) {
    showMatrixCoverageTest();
  }
}

void loop() {
  receivePacket();
}
