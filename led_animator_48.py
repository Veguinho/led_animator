#!/usr/bin/env python3
"""Convert a video into a 48x48 RGB LED animation."""

from __future__ import annotations

import sys

import led_animator as _animator


GRID_SIZE = 48


def main(argv: list[str] | None = None) -> int:
    """Run the shared animator configured for a 48x48 RGB grid."""
    previous_grid_size = _animator.GRID_SIZE
    _animator.GRID_SIZE = GRID_SIZE
    try:
        return _animator.main(
            argv,
            default_led_size=8,
            default_gap=2,
            default_output_suffix="_48x48",
        )
    finally:
        _animator.GRID_SIZE = previous_grid_size


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
