import colorsys
import unittest
from unittest import mock

import numpy as np

from audio_palette_controls import PaletteControls, default_settings, make_palette
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, SAMPLE_RATE


class AdaptivePaletteTests(unittest.TestCase):
    def visualizer(self, style="wave", **settings):
        controls = PaletteControls()
        controls.update(default_settings() | {"preset": "adaptive", "brightness": 1, "slowdown": 0} | settings)
        return AudioVisualizer(style, controls=controls)

    def signal(self, bass=0, mids=0, highs=0):
        t = np.arange(FFT_SIZE) / SAMPLE_RATE
        return sum(amplitude * np.sin(2 * np.pi * SAMPLE_RATE / FFT_SIZE * index * t)
                   for amplitude, index in zip((bass, mids, highs), (5, 43, 256)))

    def advance(self, visualizer, signal, seconds=5, fps=30, start=0):
        with mock.patch("system_audio_visualizer.time.monotonic") as clock:
            for index in range(round(seconds * fps) + 1):
                clock.return_value = start + index / fps
                frame = visualizer.render(signal)
        return frame, np.array(visualizer.controls.state()["palette"])

    def hsv(self, palette):
        return np.array([colorsys.rgb_to_hsv(*(color / 255)) for color in palette])

    def test_quiet_audio_is_cool_dark_and_muted_regardless_of_pitch(self):
        for style in ("wave", "spectrum"):
            for bands in ((.002, 0, 0), (0, .002, 0), (0, 0, .002), (.001, .001, .001)):
                with self.subTest(style=style, bands=bands):
                    _, palette = self.advance(self.visualizer(style), self.signal(*bands))
                    hsv = self.hsv(palette)
                    self.assertTrue(np.all((hsv[:, 0] >= .42) & (hsv[:, 0] <= .67)))
                    self.assertLess(hsv[:, 1].max(), .65)
                    self.assertLess(palette.max(), 80)

    def test_energetic_audio_is_warm_and_colorful_even_when_highs_dominate(self):
        for style in ("wave", "spectrum"):
            for bands in ((.5, 0, 0), (0, 0, .5), (.3, .3, .3), (.04, .15, .6)):
                with self.subTest(style=style, bands=bands):
                    _, palette = self.advance(self.visualizer(style), self.signal(*bands))
                    hsv = self.hsv(palette)
                    warm = (hsv[:, 0] <= .17) | (hsv[:, 0] >= .85)
                    self.assertGreaterEqual(np.count_nonzero(warm), 10)
                    self.assertGreater(hsv[:, 1].min(), .95)
                    self.assertGreater(palette.max(), 245)
                    self.assertGreaterEqual(len(np.unique((hsv[:, 0] * 12).astype(int))), 6)

    def test_temperature_follows_level_instead_of_bass_treble_balance(self):
        _, bass = self.advance(self.visualizer(), self.signal(bass=.08))
        _, highs = self.advance(self.visualizer(), self.signal(highs=.08))
        np.testing.assert_allclose(bass, highs, atol=1)
        ocean = make_palette(default_settings() | {
            "preset": "ocean", "brightness": .28, "saturation": .6,
        })
        _, quiet = self.advance(self.visualizer(), self.signal(bass=.002))
        np.testing.assert_allclose(quiet, ocean, atol=1)

    def test_six_db_drop_returns_to_ocean_even_in_a_loud_master(self):
        for gain in (1, .1):
            with self.subTest(gain=gain):
                visualizer = self.visualizer(slowdown=20)
                chorus = self.signal(.4, .2, .1) * gain
                _, warm = self.advance(visualizer, chorus, seconds=8)
                _, cool = self.advance(visualizer, chorus * .5, seconds=10, start=8)
                self.assertGreater(warm.max(), 245)
                self.assertLess(cool.max(), 80)
                self.assertLess(visualizer._energy, .03)
                ocean_hues = self.hsv(make_palette(default_settings() | {"preset": "ocean"}))[:, 0]
                np.testing.assert_allclose(self.hsv(cool)[:, 0], ocean_hues, atol=.04)
                _, warm_again = self.advance(visualizer, chorus, seconds=8, start=18)
                self.assertGreater(warm_again.max(), 245)

    def test_same_peak_with_sparse_energy_is_cooler_than_sustained_audio(self):
        visualizer = self.visualizer()
        chorus = self.signal(highs=.5)
        self.advance(visualizer, chorus)
        sparse = np.zeros(FFT_SIZE)
        sparse[FFT_SIZE//2] = .5
        sparse[FFT_SIZE//2 + 1] = -.5
        self.assertAlmostEqual(np.max(np.abs(chorus)), np.max(np.abs(sparse)))
        _, cool = self.advance(visualizer, sparse, seconds=8, start=5)
        self.assertLess(visualizer._energy, .03)
        self.assertLess(cool.max(), 80)

    def test_old_maxima_expire_so_a_quieter_new_song_can_adapt(self):
        visualizer = self.visualizer()
        self.advance(visualizer, self.signal(bass=.5))
        _, cool = self.advance(visualizer, self.signal(bass=.08), start=5)
        self.assertLess(cool.max(), 80)
        _, warm = self.advance(visualizer, self.signal(bass=.08), seconds=35, start=10)
        self.assertGreater(warm.max(), 245)

    def test_energy_changes_ease_brightness_and_colors_in_both_directions(self):
        quiet, energetic = self.signal(bass=.002), self.signal(.6, .23, .1)
        for style in ("wave", "spectrum"):
            visualizer = self.visualizer(style)
            quiet_frame, palette = self.advance(visualizer, quiet)
            for start, signal in ((5, energetic), (10, quiet)):
                for index in range(1, 151):
                    frame, next_palette = self.advance(
                        visualizer, signal, seconds=0, start=start + index/30)
                    self.assertLess(np.abs(next_palette - palette).max(), 35)
                    palette = next_palette
                if start == 5:
                    self.assertGreater(palette.max(), 245)
                    self.assertGreater(frame.max(), quiet_frame.max() * 3)
                else:
                    self.assertLess(palette.max(), 80)
                    self.assertLess(self.hsv(palette)[:, 1].max(), .65)

    def test_color_envelopes_are_frame_rate_independent_and_slowdown_is_smoother(self):
        palettes = []
        for fps in (30, 60):
            _, palette = self.advance(self.visualizer(), self.signal(highs=.5), seconds=1, fps=fps)
            palettes.append(palette)
        np.testing.assert_allclose(*palettes, atol=1)
        slow = self.visualizer(slowdown=95)
        self.advance(slow, self.signal(highs=.5), seconds=1)
        self.assertLess(slow._energy, .3)

    def test_colors_update_between_slow_frames_and_preview_matches_output(self):
        visualizer = self.visualizer(slowdown=95)
        white = np.full((16, 16, 3), 255, dtype=np.uint8)
        with mock.patch.object(visualizer, "_render_frame", return_value=white) as render:
            self.advance(visualizer, self.signal(highs=.5), seconds=0)
            first, _ = self.advance(visualizer, self.signal(highs=.5), seconds=0, start=1/30)
            frame, palette = self.advance(visualizer, self.signal(highs=.5), seconds=0, start=2/30)
        self.assertEqual(render.call_count, 2)
        self.assertFalse(np.array_equal(first, frame))
        np.testing.assert_array_equal(frame, np.broadcast_to(palette, frame.shape))
        np.testing.assert_array_equal(visualizer.controls.state()["frame"], frame)

    def test_controls_silence_and_switch_back(self):
        signal = self.signal(bass=.5)
        _, palette = self.advance(self.visualizer(), signal)
        _, reversed_palette = self.advance(self.visualizer(reverse=True), signal)
        np.testing.assert_array_equal(reversed_palette, palette[::-1])
        frame, gray = self.advance(self.visualizer(saturation=0, brightness=.5), signal)
        np.testing.assert_array_equal(gray, np.full((16, 3), 127))
        self.assertGreater(frame.max(), 0)
        dark, _ = self.advance(self.visualizer(brightness=0), signal)
        self.assertFalse(np.any(dark))
        for style in ("wave", "spectrum"):
            visualizer = self.visualizer(style)
            self.assertEqual(visualizer.controls.state()["settings"]["blend"], "gradient")
            frame, _ = self.advance(visualizer, self.signal())
            self.assertFalse(np.any(frame))
            _, before = self.advance(visualizer, signal, start=6)
            frame, after = self.advance(visualizer, self.signal(), start=12)
            self.assertLess(after.max(), before.max() * .35)
            self.assertFalse(np.any(frame))
            visualizer.controls.update(default_settings() | {"preset": "rainbow"})
            self.advance(visualizer, signal, seconds=0, start=18)
            self.assertFalse(visualizer._adaptive)
            self.assertFalse(np.all(visualizer.palette == 255))


if __name__ == "__main__":
    unittest.main()
