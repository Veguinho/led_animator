#include <FastLED.h>

// ESP32-S3 UNO comum: pino físico D10 costuma corresponder ao GPIO10.
// Se você conectou o DIN em outro GPIO, altere este número.
constexpr uint8_t DATA_PIN = 10;

// Uma matriz 16x16 possui 256 LEDs.
constexpr uint16_t NUM_LEDS = 256;

// Teste seguro: somente um LED aceso por vez, com brilho baixo.
constexpr uint8_t TEST_BRIGHTNESS = 20;
constexpr uint16_t STEP_DELAY_MS = 60;
 
CRGB leds[NUM_LEDS];

void setup() {
  Serial.begin(115200);
  delay(1500);

  FastLED.addLeds<WS2812B, DATA_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(TEST_BRIGHTNESS);

  // Limite adicional para o teste inicial. Isto não substitui uma fonte
  // externa adequada quando vários LEDs forem acesos simultaneamente.
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 300);

  FastLED.clear(true);
  Serial.println("Teste WS2812 iniciado: um LED por vez.");
}

void loop() {
  static uint16_t pixel = 0;
  static uint8_t colorIndex = 0;

  const CRGB testColors[] = {
    CRGB::Red,
    CRGB::Green,
    CRGB::Blue,
    CRGB::White
  };

  FastLED.clear();
  leds[pixel] = testColors[colorIndex];
  FastLED.show();

  Serial.printf("Pixel %u, cor %u\n", pixel, colorIndex);
  delay(STEP_DELAY_MS);

  colorIndex++;
  if (colorIndex >= 4) {
    colorIndex = 0;
    pixel++;

    if (pixel >= NUM_LEDS) {
      pixel = 0;
      FastLED.clear(true);
      delay(500);
    }
  }
}
