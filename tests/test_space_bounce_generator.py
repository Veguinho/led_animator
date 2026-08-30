import unittest

import numpy as np

from generate_space_bounce import (
    DEFAULT_FPS,
    bounce_position,
    build_parser,
    generate_frames,
)


class SpaceBounceGeneratorTests(unittest.TestCase):
    def test_defaults_to_a_literal_16x16_60_fps_mp4(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.fps, DEFAULT_FPS)
        self.assertEqual(args.fps, 60.0)
        self.assertEqual(args.output.suffix, ".mp4")

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

    def test_every_frame_contains_only_the_ball_and_red_trail_on_black(self):
        frames = generate_frames(30)
        white_pixels = np.all(frames == 255, axis=3)
        red_pixels = (
            (frames[..., 0] > 0)
            & (frames[..., 0] < 255)
            & (frames[..., 1] == 0)
            & (frames[..., 2] == 0)
        )
        valid_pixels = white_pixels | red_pixels | np.all(frames == 0, axis=3)

        self.assertTrue(np.all(np.sum(white_pixels, axis=(1, 2)) == 5))
        self.assertTrue(np.all(np.any(red_pixels, axis=(1, 2))))
        self.assertTrue(np.all(valid_pixels))

    def test_red_trail_contains_multiple_fading_intensities(self):
        frame = generate_frames(360)[180]
        red_only = frame[(frame[..., 1] == 0) & (frame[..., 2] == 0)]
        intensities = np.unique(red_only[:, 0])
        visible_intensities = intensities[intensities > 0]
        self.assertGreaterEqual(len(visible_intensities), 2)


if __name__ == "__main__":
    unittest.main()
