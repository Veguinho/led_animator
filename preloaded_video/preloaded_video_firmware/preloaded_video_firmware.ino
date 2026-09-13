#include <FastLED.h>
#include "platforms/esp/32/drivers/lcd_spi/bus_traits.h"
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include "panel_layout.h"
#include "clip_buffer.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !defined(BOARD_HAS_PSRAM)
#error "Select ESP32-S3 with OPI PSRAM enabled (PSRAM=opi)."
#endif

// Same existing CH340 socket, pin mapping and total power budget as live mode.
#define PANEL_SERIAL Serial0
constexpr uint8_t BRIGHTNESS = 24;
constexpr uint32_t MAX_POWER_MILLIAMPS = 2000;
constexpr uint32_t SERIAL_BAUD = 230400;
uint64_t baudConfirmationDeadline = 0;
constexpr uint32_t MAX_CLIP_BYTES = 6 * 1024 * 1024;
constexpr uint16_t MAX_PAYLOAD = 4096 + 4;
CRGB leds[NUM_LEDS];
uint8_t payload[MAX_PAYLOAD];
ClipBuffer clip;
PlaybackClock playback;
uint32_t lastPlaySequence = 0;
bool hasPlaySequence = false;
constexpr uint8_t CMD_INFO = 1, CMD_BEGIN = 2, CMD_CHUNK = 3, CMD_PLAY = 4, CMD_STOP = 5, CMD_STATUS = 6;
constexpr uint8_t CMD_BAUD = 7, CMD_VERIFY = 8;

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

CRGB decodeRgb565(uint16_t color) {
  const uint8_t red5 = (color >> 11) & 0x1f;
  const uint8_t green6 = (color >> 5) & 0x3f;
  const uint8_t blue5 = color & 0x1f;
  return CRGB(
      (red5 << 3) | (red5 >> 2),
      (green6 << 2) | (green6 >> 4),
      (blue5 << 3) | (blue5 >> 2));
}

void drawFrame(const uint8_t *payload) {
  // Wait only after reception/CRC validation, so USB reception overlaps DMA.
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
}

void reply(uint16_t error, uint32_t sequence, uint32_t value = 0) {
  uint8_t bytes[24] = {'P', 'V', 'O', 'K', 1, static_cast<uint8_t>(error ? 2 : 1)};
  writeU16(bytes + 6, error);
  writeU32(bytes + 8, sequence);
  writeU32(bytes + 12, value);
  writeU32(bytes + 16, playback.displayed);
  writeU32(bytes + 20, playback.missed);
  PANEL_SERIAL.write(bytes, sizeof(bytes));
}

void command(uint8_t type, uint16_t length, uint32_t sequence) {
  switch (type) {
    case CMD_INFO:
      if (length) { reply(1, sequence); return; }
      baudConfirmationDeadline = 0;
      reply(clip.data ? 0 : 6, sequence, clip.capacity);
      break;
    case CMD_BAUD: {
      if (length != 4 || playback.playing) { reply(1, sequence); return; }
      const uint32_t rate = readU32(payload);
      if (rate != 230400 && rate != 460800 && rate != 921600 &&
          rate != 1000000 && rate != 1500000 && rate != 2000000) {
        reply(1, sequence); return;
      }
      // Acknowledge at the old speed, then require INFO at the new speed.
      // A failed speed test returns to the known boot rate without reflashing.
      reply(0, sequence, rate);
      PANEL_SERIAL.flush();
      delay(20);
      PANEL_SERIAL.updateBaudRate(rate);
      baudConfirmationDeadline = esp_timer_get_time() + 1500000;
      break;
    }
    case CMD_VERIFY:
      if (length) { reply(1, sequence); return; }
      reply(clip.complete() ? 0 : 5, sequence, clip.received);
      break;
    case CMD_BEGIN:
      if (length != 12) { reply(1, sequence); return; }
      if (!clip.data) { reply(6, sequence); return; }
      playback.playing = false;
      FastLED.wait();
      if (!clip.begin(readU32(payload), readU32(payload + 4), readU32(payload + 8))) {
        reply(3, sequence); return;
      }
      hasPlaySequence = false;
      reply(0, sequence, 0);
      break;
    case CMD_CHUNK:
      if (playback.playing || length <= 4 ||
          !clip.append(readU32(payload), payload + 4, length - 4)) {
        reply(4, sequence); return;
      }
      reply(0, sequence, clip.received);
      break;
    case CMD_PLAY:
      if (length != 1 || payload[0] > 1) { reply(1, sequence); return; }
      if (!clip.complete()) { reply(5, sequence); return; }
      // Retried CMD_PLAY must not restart the clip after a lost acknowledgement.
      if (!hasPlaySequence || lastPlaySequence != sequence) {
        playback.play(esp_timer_get_time(), payload[0]);
        lastPlaySequence = sequence;
        hasPlaySequence = true;
      }
      reply(0, sequence, clip.fps);
      break;
    case CMD_STOP:
      if (length) { reply(1, sequence); return; }
      playback.playing = false;
      FastLED.wait();
      FastLED.clear(true);
      FastLED.wait();
      reply(0, sequence);
      break;
    case CMD_STATUS:
      if (length) { reply(1, sequence); return; }
      reply(0, sequence, playback.playing);
      break;
    default:
      reply(1, sequence);
  }
}

void receivePacket() {
  static uint8_t matched = 0;
  const uint8_t magic[] = {'P', 'V', '3', '2'};
  while (PANEL_SERIAL.available()) {
    uint8_t byte = PANEL_SERIAL.read();
    if (byte == magic[matched]) ++matched;
    else matched = byte == magic[0] ? 1 : 0;
    if (matched != 4) continue;
    matched = 0;
    uint8_t header[12];
    if (PANEL_SERIAL.readBytes(header, sizeof(header)) != sizeof(header)) return;
    const uint16_t length = readU16(header + 2);
    const uint32_t sequence = readU32(header + 4);
    if (header[0] != 1 || length > MAX_PAYLOAD) { reply(1, sequence); return; }
    if (PANEL_SERIAL.readBytes(payload, length) != length) { reply(1, sequence); return; }
    if ((updateCrc(0xffffffffUL, payload, length) ^ 0xffffffffUL) != readU32(header + 8)) {
      reply(2, sequence); return;
    }
    command(header[1], length, sequence);
    return;
  }
}

void setup() {
  PANEL_SERIAL.setRxBufferSize(2 * (MAX_PAYLOAD + 16));
  PANEL_SERIAL.begin(SERIAL_BAUD);
  PANEL_SERIAL.setTimeout(500);
  FastLED.setExclusiveDriver<fl::Bus::LCD_CLOCKLESS>();
  // Legacy addLeds<..., Bus> only links the requested driver in FastLED
  // 3.10.5; its controllers still bind to RMT. Select LCD on each channel.
  fl::ChannelOptions options;
  options.mBus = fl::Bus::LCD_CLOCKLESS;
  for (uint8_t panel = 0; panel < PANEL_COUNT; ++panel) {
    FastLED.add(fl::ChannelConfig(
        fl::makeClockless<fl::TIMING_WS2812_800KHZ>(DATA_PINS[panel]),
        fl::span<CRGB>(leds + panel * PANEL_LEDS, PANEL_LEDS), GRB, options));
  }
  FastLED.setBrightness(BRIGHTNESS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, MAX_POWER_MILLIAMPS);
  FastLED.clear(true);

  FastLED.wait();
  // Allocate after the LED driver has reserved its DMA buffers. Leave headroom.
  uint32_t available = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  uint32_t budget = available > 512 * 1024 ? available - 512 * 1024 : 0;
  if (budget > MAX_CLIP_BYTES) budget = MAX_CLIP_BYTES;
  budget = budget / FRAME_BYTES * FRAME_BYTES;
  if (budget) {
    clip.data = static_cast<uint8_t *>(heap_caps_malloc(budget, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (clip.data) clip.capacity = budget;
  }
}

void loop() {
  if (baudConfirmationDeadline && esp_timer_get_time() > baudConfirmationDeadline) {
    PANEL_SERIAL.updateBaudRate(SERIAL_BAUD);
    baudConfirmationDeadline = 0;
  }
  receivePacket();
  const int32_t frame = playback.next(esp_timer_get_time(), clip.fps, clip.frames);
  if (frame >= 0) drawFrame(clip.data + static_cast<uint32_t>(frame) * FRAME_BYTES);
  delay(1);
}
