#pragma once
#include <stdint.h>
#include <string.h>
#include "panel_layout.h"

constexpr uint32_t FRAME_BYTES = NUM_LEDS * 2;

inline uint32_t updateCrc(uint32_t crc, const uint8_t *data, uint32_t length) {
  for (uint32_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc >> 1) ^ (0xedb88320UL & -(crc & 1UL));
  }
  return crc;
}

struct ClipBuffer {
  uint8_t *data = nullptr;
  uint32_t capacity = 0, frames = 0, fps = 0, received = 0;
  uint32_t expectedCrc = 0, crc = 0xffffffffUL;

  bool begin(uint32_t count, uint32_t rate, uint32_t checksum) {
    if (!data || !count || rate < 1 || rate > 60 || count > capacity / FRAME_BYTES)
      return false;
    frames = count;
    fps = rate;
    received = 0;
    expectedCrc = checksum;
    crc = 0xffffffffUL;
    return true;
  }

  bool append(uint32_t offset, const uint8_t *bytes, uint32_t length) {
    if (!frames || !length || offset > frames * FRAME_BYTES ||
        length > frames * FRAME_BYTES - offset) return false;
    if (offset < received) {
      // Lost ACK: accept an exact duplicate without advancing the checksum.
      return length <= received - offset && memcmp(data + offset, bytes, length) == 0;
    }
    if (offset != received) return false;
    memcpy(data + offset, bytes, length);
    crc = updateCrc(crc, bytes, length);
    received += length;
    return true;
  }

  bool complete() const {
    return frames && received == frames * FRAME_BYTES &&
           (crc ^ 0xffffffffUL) == expectedCrc;
  }
};

struct PlaybackClock {
  uint64_t start = 0, lastSlot = 0;
  uint32_t displayed = 0, missed = 0;
  bool playing = false, loop = true, hasFrame = false;

  void play(uint64_t now, bool repeat) {
    start = now;
    lastSlot = 0;
    displayed = missed = 0;
    playing = true;
    loop = repeat;
    hasFrame = false;
  }

  int32_t next(uint64_t now, uint32_t fps, uint32_t frames) {
    if (!playing || !frames) return -1;
    const uint64_t slot = (now - start) * fps / 1000000ULL;
    if (hasFrame && slot == lastSlot) return -1;
    if (!loop && slot >= frames) {
      playing = false;  // Keep the last displayed frame lit.
      return -1;
    }
    missed += hasFrame ? slot - lastSlot - 1 : slot;
    lastSlot = slot;
    hasFrame = true;
    ++displayed;
    return slot % frames;
  }
};
