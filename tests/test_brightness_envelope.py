import unittest

import numpy as np

from brightness_envelope import BrightnessEnvelope
from export_arduino import encode_rgb565


class BrightnessEnvelopeTests(unittest.TestCase):
    def test_settled_test_red_keeps_its_rgb565_level(self):
        envelope = BrightnessEnvelope()
        red = np.full((32, 32, 3), (16, 0, 0), dtype=np.uint8)
        for index in range(100):
            result = envelope.apply(red, index / 20)
        self.assertEqual(encode_rgb565(result), encode_rgb565(red))

    def test_constant_input_has_no_brightness_cycle(self):
        envelope = BrightnessEnvelope()
        frame = np.full((32, 32, 3), (90, 45, 0), dtype=np.uint8)
        settled = []
        for index in range(481):
            result = envelope.apply(frame, index / 20)
            if index >= 240:
                settled.append(result)
        for result in settled:
            np.testing.assert_allclose(result, frame, atol=1)
            np.testing.assert_array_equal(result, settled[0])

    def test_dim_input_is_not_automatically_boosted(self):
        dim, bright = BrightnessEnvelope(), BrightnessEnvelope()
        low = np.full((32, 32, 3), (10, 0, 0), dtype=np.uint8)
        high = np.full((32, 32, 3), (100, 0, 0), dtype=np.uint8)
        for index in range(201):
            a = dim.apply(low, index / 20)
            b = bright.apply(high, index / 20)
            self.assertLessEqual(int(a.max()), 10)
        np.testing.assert_allclose(a, low, atol=1)
        np.testing.assert_allclose(b, high, atol=1)
        self.assertGreater(int(b.max()), 5 * int(a.max()))

    def test_white_flash_is_limited_and_silence_fades_out(self):
        envelope = BrightnessEnvelope()
        black = np.zeros((32, 32, 3), dtype=np.uint8)
        white = np.full_like(black, 255)
        envelope.apply(black, 0)
        flash = envelope.apply(white, .05)
        self.assertLessEqual(int(flash.max()), 25)
        for index in range(2, 100):
            result = envelope.apply(black, index / 20)
        self.assertFalse(np.any(result))
