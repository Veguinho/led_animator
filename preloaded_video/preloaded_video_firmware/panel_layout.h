#pragma once

#include <stdint.h>

constexpr uint8_t PANEL_WIDTH = 16;
constexpr uint8_t PANEL_HEIGHT = 16;
constexpr uint8_t PANEL_COLUMNS = 2;
constexpr uint8_t PANEL_ROWS = 2;
constexpr uint8_t PANEL_COUNT = PANEL_COLUMNS * PANEL_ROWS;
constexpr uint8_t DATA_PINS[PANEL_COUNT] = {10, 11, 12, 13};
constexpr uint16_t PANEL_LEDS = PANEL_WIDTH * PANEL_HEIGHT;
constexpr uint8_t WIDTH = PANEL_WIDTH * PANEL_COLUMNS;
constexpr uint8_t HEIGHT = PANEL_HEIGHT * PANEL_ROWS;
constexpr uint16_t NUM_LEDS = WIDTH * HEIGHT;

// Viewed from the front, with each panel's pixel 0 at its top-left:
//   IO10 IO11
//   IO12 IO13
// Every panel has its own horizontal serpentine chain (even rows go right).
constexpr bool SERPENTINE_LAYOUT = true;
constexpr bool FLIP_HORIZONTAL = false;
constexpr bool FLIP_VERTICAL = false;

inline uint16_t physicalIndex(uint8_t row, uint8_t column) {
  if (FLIP_VERTICAL) {
    row = HEIGHT - 1 - row;
  }
  if (FLIP_HORIZONTAL) {
    column = WIDTH - 1 - column;
  }
  const uint8_t panel = (row / PANEL_HEIGHT) * PANEL_COLUMNS + column / PANEL_WIDTH;
  const uint8_t localRow = row % PANEL_HEIGHT;
  uint8_t localColumn = column % PANEL_WIDTH;
  if (SERPENTINE_LAYOUT && (localRow & 1U)) {
    localColumn = PANEL_WIDTH - 1 - localColumn;
  }
  return panel * PANEL_LEDS + localRow * PANEL_WIDTH + localColumn;
}
