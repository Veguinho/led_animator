#!/usr/bin/env python3
"""Check sustained video upload throughput without displaying test data."""

import argparse
import json
from pathlib import Path
import time

import numpy as np

try:
    from . import player
except ImportError:
    import player


class CountingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.writes = 0

    def write(self, data):
        self.writes += 1
        return self.connection.write(data)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, choices=player.BAUD_RATES, default=player.DEFAULT_BAUD)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    player.stop_mode_workers()
    connection = player.connect_video(player.resolve_port(args.port, wait_timeout=30), args.baud)
    counted = CountingConnection(connection)
    results = []
    try:
        rng = np.random.default_rng(42)
        for index in range(args.rounds):
            clip = player.Clip(rng.integers(0, 256, 512 * player.FRAME_BYTES, dtype=np.uint8).tobytes(), 30)
            writes = counted.writes
            started = time.monotonic()
            player.preload(counted, clip, play=False)
            elapsed = time.monotonic() - started
            expected = (len(clip.data) + player.CHUNK_BYTES - 1) // player.CHUNK_BYTES + 3
            result = dict(round=index + 1, baud=args.baud, bytes=len(clip.data),
                          seconds=elapsed, bytes_per_second=len(clip.data) / elapsed,
                          retries=counted.writes - writes - expected, crc_verified=True)
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        connection.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
