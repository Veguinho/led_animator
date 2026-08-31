import unittest

import numpy as np

from lava_lamp_stream import (
    DEFAULT_SIMULATION_SIZE,
    LavaLampFluid,
    build_parser,
    iter_lava_frames,
)


class LavaLampFluidTests(unittest.TestCase):
    def test_defaults_match_the_16x16_stream(self):
        args = build_parser().parse_args([])

        self.assertEqual(args.fps, 30.0)
        self.assertEqual(args.simulation_size, DEFAULT_SIMULATION_SIZE)
        self.assertEqual(args.simulation_size % 16, 0)

    def test_render_is_a_16x16_warm_rgb_frame(self):
        simulation = LavaLampFluid(size=32, warmup_seconds=1.0, seed=7)
        frame = simulation.render()

        self.assertEqual(frame.shape, (16, 16, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertTrue(np.any(frame[..., 0]))
        self.assertFalse(np.any(frame[..., 2]))
        self.assertTrue(np.all(frame[..., 0] >= frame[..., 1]))

    def test_palette_contains_red_orange_and_yellow_lava(self):
        frame = LavaLampFluid(size=32, seed=2026).render()
        red = frame[..., 0]
        green = frame[..., 1]

        red_pixels = (red > 20) & (green < red * 0.25)
        orange_pixels = (red > 80) & (green >= red * 0.25) & (green < red * 0.70)
        yellow_pixels = (red > 100) & (green >= red * 0.70)
        self.assertTrue(np.any(red_pixels))
        self.assertTrue(np.any(orange_pixels))
        self.assertTrue(np.any(yellow_pixels))

    def test_seed_makes_the_fluid_deterministic(self):
        first = LavaLampFluid(size=32, warmup_seconds=0.5, seed=42)
        second = LavaLampFluid(size=32, warmup_seconds=0.5, seed=42)

        for _ in range(4):
            np.testing.assert_array_equal(first.render(), second.render())

    def test_fluid_moves_between_frames(self):
        simulation = LavaLampFluid(size=32, warmup_seconds=1.5, seed=11)
        first = simulation.render()
        for _ in range(8):
            last = simulation.render()

        self.assertGreater(np.count_nonzero(first != last), 5)

    def test_stream_frame_is_one_rgb565_panel(self):
        simulation = LavaLampFluid(size=32, warmup_seconds=0.25)
        payload = next(iter_lava_frames(simulation))

        self.assertEqual(len(payload), 16 * 16 * 2)

    def test_rejects_grid_that_cannot_downsample_to_panel(self):
        with self.assertRaisesRegex(ValueError, "multiple of 16"):
            LavaLampFluid(size=24)


if __name__ == "__main__":
    unittest.main()
