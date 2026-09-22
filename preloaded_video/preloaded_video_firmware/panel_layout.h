#pragma once

#include <stdint.h>

constexpr uint8_t PANEL_WIDTH = 16;
constexpr uint8_t PANEL_HEIGHT = 16;
constexpr uint8_t PANEL_COLUMNS = 3;
constexpr uint8_t PANEL_ROWS = 3;
constexpr uint8_t PANEL_COUNT = PANEL_COLUMNS * PANEL_ROWS;
constexpr uint8_t DATA_PINS[PANEL_COUNT] = {
    11, 10, 9,
    13, 12, 20,
    46, 17, 18,
};
// All nine panels are mounted with the same position and rotation, so every
// tile uses the same clockwise rotation and horizontal mirror correction.
constexpr bool PANEL_ROTATE_CLOCKWISE = true;
constexpr bool PANEL_MIRROR_HORIZONTAL = true;
constexpr uint16_t PANEL_LEDS = PANEL_WIDTH * PANEL_HEIGHT;
constexpr uint8_t WIDTH = PANEL_WIDTH * PANEL_COLUMNS;
constexpr uint8_t HEIGHT = PANEL_HEIGHT * PANEL_ROWS;
constexpr uint16_t NUM_LEDS = WIDTH * HEIGHT;

// Viewed from the front, with each panel's pixel 0 at its top-left:
//   IO11 IO10 IO09
//   IO13 IO12 IO20
//   IO46 IO17 IO18
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
  const uint8_t sourceLocalRow = row % PANEL_HEIGHT;
  const uint8_t sourceLocalColumn = column % PANEL_WIDTH;
  const uint8_t localRow = PANEL_ROTATE_CLOCKWISE
      ? sourceLocalColumn
      : sourceLocalRow;
  uint8_t localColumn = PANEL_ROTATE_CLOCKWISE
      ? PANEL_WIDTH - 1 - sourceLocalRow
      : sourceLocalColumn;
  if (PANEL_MIRROR_HORIZONTAL) {
    localColumn = PANEL_WIDTH - 1 - localColumn;
  }
  if (SERPENTINE_LAYOUT && (localRow & 1U)) {
    localColumn = PANEL_WIDTH - 1 - localColumn;
  }
  return panel * PANEL_LEDS + localRow * PANEL_WIDTH + localColumn;
}
