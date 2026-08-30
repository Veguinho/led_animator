import json
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from export_arduino import (
    HEADER,
    MAGIC,
    PIXEL_FORMAT_RGB565_LE,
    build_binary,
    load_animation,
    sample_animation,
)


class ArduinoExportTests(unittest.TestCase):
    def test_json_is_sampled_and_encoded_as_rgb565(self):
        frames = np.zeros((10, 16, 16, 3), dtype=np.uint8)
        frames[0, 0, 0] = [255, 255, 255]
        frames[5, 0, 0] = [255, 0, 0]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ledmap.json"
            path.write_text(
                json.dumps(
                    {
                        "width": 16,
                        "height": 16,
                        "fps": 10,
                        "frames": frames.tolist(),
                    }
                )
            )
            sampled = sample_animation(load_animation(path), 2)
            encoded = build_binary(sampled)

        (
            magic,
            version,
            width,
            height,
            pixel_format,
            frame_count,
            frame_duration_us,
            expected_crc,
        ) = HEADER.unpack(encoded[: HEADER.size])
        payload = encoded[HEADER.size :]

        self.assertEqual(magic, MAGIC)
        self.assertEqual(version, 1)
        self.assertEqual((width, height), (16, 16))
        self.assertEqual(pixel_format, PIXEL_FORMAT_RGB565_LE)
        self.assertEqual(frame_count, 2)
        self.assertEqual(frame_duration_us, 500_000)
        self.assertEqual(expected_crc, zlib.crc32(payload))
        self.assertEqual(len(payload), 2 * 16 * 16 * 2)
        self.assertEqual(payload[:2], b"\xff\xff")
        self.assertEqual(payload[16 * 16 * 2 : 16 * 16 * 2 + 2], b"\x00\xf8")

    def test_rejects_non_16x16_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ledmap.json"
            path.write_text(
                json.dumps({"width": 48, "height": 48, "fps": 1, "frames": []})
            )
            with self.assertRaisesRegex(ValueError, "requires a 16x16 map"):
                load_animation(path)


if __name__ == "__main__":
    unittest.main()
