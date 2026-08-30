#include <FastLED.h>

#include "cs2_16x16_animation.h"

constexpr uint8_t DATA_PIN = 10;
constexpr uint8_t WIDTH = 16;
constexpr uint8_t HEIGHT = 16;
constexpr uint16_t NUM_LEDS = WIDTH * HEIGHT;
constexpr uint8_t BRIGHTNESS = 32;
constexpr bool SERPENTINE_LAYOUT = true;

constexpr uint32_t HEADER_SIZE = 24;
constexpr uint32_t BYTES_PER_FRAME = NUM_LEDS * 2;

CRGB leds[NUM_LEDS];
uint32_t frameCount = 0;
uint32_t frameDurationUs = 0;
uint32_t currentFrame = 0;
uint32_t nextFrameAt = 0;
bool animationReady = false;

uint8_t animationByte(uint32_t offset) {
  return pgm_read_byte(cs2_16x16_animation + offset);
}

uint32_t animationU32(uint32_t offset) {
  return static_cast<uint32_t>(animationByte(offset)) |
         (static_cast<uint32_t>(animationByte(offset + 1)) << 8) |
         (static_cast<uint32_t>(animationByte(offset + 2)) << 16) |
         (static_cast<uint32_t>(animationByte(offset + 3)) << 24);
}

uint16_t physicalIndex(uint8_t row, uint8_t column) {
  if (SERPENTINE_LAYOUT && (row & 1)) {
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

bool readAnimationHeader() {
  const uint8_t expectedMagic[8] = {'L', 'E', 'D', 'A', 'N', 'I', 'M', 0};
  for (uint8_t index = 0; index < sizeof(expectedMagic); ++index) {
    if (animationByte(index) != expectedMagic[index]) {
      Serial.println("Invalid LED animation magic.");
      return false;
    }
  }

  const uint8_t version = animationByte(8);
  const uint8_t width = animationByte(9);
  const uint8_t height = animationByte(10);
  const uint8_t pixelFormat = animationByte(11);
  frameCount = animationU32(12);
  frameDurationUs = animationU32(16);

  if (version != 1 || width != WIDTH || height != HEIGHT || pixelFormat != 1 ||
      frameCount == 0 || frameDurationUs == 0) {
    Serial.println("Unsupported LED animation header.");
    return false;
  }

  const uint64_t requiredSize = HEADER_SIZE +
      static_cast<uint64_t>(frameCount) * BYTES_PER_FRAME;
  if (requiredSize != cs2_16x16_animation_size) {
    Serial.println("LED animation payload size does not match its header.");
    return false;
  }
  return true;
}

void drawFrame(uint32_t frameIndex) {
  uint32_t offset = HEADER_SIZE + frameIndex * BYTES_PER_FRAME;
  for (uint8_t row = 0; row < HEIGHT; ++row) {
    for (uint8_t column = 0; column < WIDTH; ++column) {
      const uint16_t color = static_cast<uint16_t>(animationByte(offset)) |
                             (static_cast<uint16_t>(animationByte(offset + 1)) << 8);
      offset += 2;
      leds[physicalIndex(row, column)] = decodeRgb565(color);
    }
  }
  FastLED.show();
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  FastLED.addLeds<WS2812B, DATA_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 300);
  FastLED.clear(true);

  animationReady = readAnimationHeader();
  if (animationReady) {
    Serial.printf(
        "Playing %lu frames every %lu us.\n",
        static_cast<unsigned long>(frameCount),
        static_cast<unsigned long>(frameDurationUs));
    nextFrameAt = micros();
  }
}

void loop() {
  if (!animationReady) {
    delay(1000);
    return;
  }

  const uint32_t now = micros();
  if (static_cast<int32_t>(now - nextFrameAt) < 0) {
    return;
  }

  drawFrame(currentFrame);
  currentFrame = (currentFrame + 1) % frameCount;
  nextFrameAt += frameDurationUs;

  // Avoid a long catch-up burst after a debugger pause or timer stall.
  if (static_cast<int32_t>(micros() - nextFrameAt) >=
      static_cast<int32_t>(frameDurationUs)) {
    nextFrameAt = micros() + frameDurationUs;
  }
}
