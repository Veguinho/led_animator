import unittest
from unittest import mock

import numpy as np

from audio_palette_controls import PaletteControls, default_settings, make_palette
from system_audio_visualizer import AudioVisualizer, FFT_SIZE


class MovingRainbowTests(unittest.TestCase):
    def settings(self, **changes):
        return default_settings() | {"preset": "moving-rainbow", "brightness": 1, "slowdown": 0} | changes

    def test_rainbow_moves_right_and_wraps_without_a_seam(self):
        initial = make_palette(self.settings())
        shifted = make_palette(self.settings(), phase=1 / 16)
        np.testing.assert_array_equal(shifted, np.roll(initial, 1, axis=0))
        np.testing.assert_array_equal(make_palette(self.settings(), phase=1), initial)
        before = make_palette(self.settings(), phase=1 - 0.0001).astype(int)
        after = make_palette(self.settings(), phase=0.0001).astype(int)
        self.assertLessEqual(np.abs(before - after).max(), 1)

    def test_fractional_motion_blends_colors_and_reverse_changes_direction(self):
        initial = make_palette(self.settings())
        half_step = make_palette(self.settings(), phase=1 / 32)
        self.assertFalse(np.array_equal(initial, half_step))
        self.assertFalse(np.array_equal(np.roll(initial, 1, axis=0), half_step))
        reversed_initial = make_palette(self.settings(reverse=True))
        reversed_shift = make_palette(self.settings(reverse=True), phase=1 / 16)
        np.testing.assert_array_equal(reversed_shift, np.roll(reversed_initial, -1, axis=0))

    def test_brightness_saturation_and_gradient_are_respected(self):
        self.assertFalse(np.any(make_palette(self.settings(brightness=0), phase=.4)))
        np.testing.assert_array_equal(make_palette(self.settings(saturation=0, brightness=.5)), np.full((16, 3), 128))
        controls = PaletteControls()
        controls.update(self.settings(blend="bands"))
        self.assertEqual(controls.state()["settings"]["blend"], "gradient")

    def test_color_motion_uses_elapsed_time_and_slowdown_at_every_output_frame(self):
        for slowdown, expected_phase in ((0, .25), (50, .125), (95, .0125)):
            with self.subTest(slowdown=slowdown):
                controls = PaletteControls()
                controls.update(self.settings(slowdown=slowdown))
                visualizer = AudioVisualizer("wave", controls=controls)
                with mock.patch.object(visualizer, "_render_frame", return_value=np.full((16, 16, 3), 255, dtype=np.uint8)), \
                     mock.patch("system_audio_visualizer.time.monotonic", side_effect=[10, 11, 12]):
                    first = visualizer.render(np.zeros(FFT_SIZE))
                    middle = visualizer.render(np.zeros(FFT_SIZE))
                    last = visualizer.render(np.zeros(FFT_SIZE))
                expected = make_palette(self.settings(), phase=expected_phase)
                self.assertAlmostEqual(visualizer._rainbow_phase, expected_phase)
                np.testing.assert_allclose(last, np.broadcast_to(expected, (16, 16, 3)), atol=1)
                self.assertFalse(np.array_equal(first, middle))
                self.assertFalse(np.array_equal(middle, last))
                np.testing.assert_allclose(controls.state()["palette"], expected, atol=1)
                np.testing.assert_array_equal(controls.state()["frame"], last)

    def test_color_motion_does_not_reset_audio_trails_and_switching_to_adaptive_clears_the_mask(self):
        controls = PaletteControls()
        controls.update(self.settings())
        visualizer = AudioVisualizer("spectrum", controls=controls)
        with mock.patch.object(visualizer._delayer, "reset", wraps=visualizer._delayer.reset) as reset:
            for _ in range(5):
                visualizer.render(np.zeros(FFT_SIZE))
            self.assertEqual(reset.call_count, 1)
            controls.update(default_settings() | {"preset": "adaptive"})
            visualizer.render(np.zeros(FFT_SIZE))
            self.assertEqual(reset.call_count, 2)
            self.assertTrue(visualizer._adaptive)
            np.testing.assert_array_equal(visualizer.palette, np.full((16, 3), 255))

    def test_silent_audio_stays_dark_in_both_styles(self):
        for style in ("wave", "spectrum"):
            controls = PaletteControls()
            controls.update(self.settings(slowdown=75))
            visualizer = AudioVisualizer(style, controls=controls)
            for _ in range(10):
                self.assertFalse(np.any(visualizer.render(np.zeros(FFT_SIZE))))


if __name__ == "__main__":
    unittest.main()
