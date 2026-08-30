import unittest

import numpy as np

from generate_space_bounce import bounce_position, generate_frames


class SpaceBounceGeneratorTests(unittest.TestCase):
    def test_generates_deterministic_16x16_rgb_frames(self):
        first = generate_frames(24, seed=7)
        second = generate_frames(24, seed=7)

        self.assertEqual(first.shape, (24, 16, 16, 3))
        self.assertEqual(first.dtype, np.uint8)
        np.testing.assert_array_equal(first, second)

    def test_orb_path_reaches_all_four_edges(self):
        positions = [bounce_position(index / 2400) for index in range(2400)]
        x_values = [position[0] for position in positions]
        y_values = [position[1] for position in positions]

        self.assertLess(min(x_values), 1.1)
        self.assertGreater(max(x_values), 13.9)
        self.assertLess(min(y_values), 1.1)
        self.assertGreater(max(y_values), 13.9)

    def test_every_frame_contains_a_white_hot_orb(self):
        frames = generate_frames(30)
        white_pixels = np.all(frames == 255, axis=3)
        self.assertTrue(np.all(np.any(white_pixels, axis=(1, 2))))


if __name__ == "__main__":
    unittest.main()
