#!/usr/bin/env python3
"""Show repeatable gray/color ramps over the normal, checksummed LED protocol."""

import argparse
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_arduino import encode_rgb565
from led_animator import map_led_intensity
from stream_arduino import (
    PACKET_CLEAR, PACKET_FRAME, STATUS_ACK, exchange_packet, handshake,
    open_serial, resolve_port,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--video", action="store_true", help="use the lower video output ceiling")
    parser.add_argument("--color", choices=("gray", "red", "green", "blue"), default="gray")
    parser.add_argument("--image", type=Path, help="show this image converted to grayscale at 48x48")
    parser.add_argument("--led-gamma", type=float, default=1.0,
                        help="1 tests raw firmware response; 2.2 matches normal video mapping")
    args = parser.parse_args()
    if not np.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be positive and finite")
    # Sixteen vertical bars, left to right: 0, 17, ... 255. Keep the frame
    # stationary so camera exposure and fractional PWM have time to settle.
    ramp = np.repeat(np.arange(16, dtype=np.uint8) * 17, 3)
    frame = np.broadcast_to(ramp[None, :, None], (48, 48, 3)).copy()
    if args.image:
        with Image.open(args.image) as image:
            frame = np.asarray(ImageOps.fit(ImageOps.grayscale(image), (48, 48),
                                           method=Image.Resampling.LANCZOS).convert("RGB")).copy()
    if args.color != "gray":
        frame[..., [c for c in range(3) if c != ("red", "green", "blue").index(args.color)]] = 0
    payload = encode_rgb565(map_led_intensity(frame, args.led_gamma))
    connection = open_serial(resolve_port(args.port), 0.5)
    try:
        time.sleep(0.2)
        connection.reset_input_buffer()
        handshake(connection, 12, 0.5, 3, 48, video_mode=args.video)
        end = time.monotonic() + args.seconds
        sequence = 1
        while time.monotonic() < end:
            exchange_packet(connection, PACKET_FRAME, sequence, payload, STATUS_ACK, 0.5, 3)
            if sequence == 1:
                print("Grayscale image acknowledged." if args.image else
                      "Gray/color ramp acknowledged; bars increase from black to full intensity.", flush=True)
            sequence += 1
            time.sleep(1 / 12)
    finally:
        try:
            exchange_packet(connection, PACKET_CLEAR, 0, b"", STATUS_ACK, 0.5, 1)
        finally:
            connection.close()


if __name__ == "__main__":
    main()
