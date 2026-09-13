import colorsys
import unittest
from unittest import mock

import numpy as np

from audio_palette_controls import PaletteControls, default_settings, make_palette
from export_arduino import encode_rgb565
from system_audio_visualizer import (
    DEFAULT_SENSITIVITY,
    FFT_SIZE,
    GRID_SIZE,
    SAMPLE_RATE,
    AudioVisualizer,
    build_parser,
    color_palette,
    iter_audio_frames,
)


class AudioVisualizerTests(unittest.TestCase):
    def test_native_48_wave_has_individual_pixels_and_full_screen_reach(self):
        for slowdown in (0, 75):
            with self.subTest(slowdown=slowdown):
                visualizer = self.visualizer("wave", size=48, slowdown=slowdown)
                for _ in range(80):
                    frame = visualizer.render(self.tone(0.7, 440))
                self.assertEqual(frame.shape, (48, 48, 3))
                self.assertEqual(frame.dtype, np.uint8)
                self.assertTrue(np.all(np.any(frame, axis=(0, 2))))
                lit_rows = np.flatnonzero(np.any(frame, axis=(1, 2)))
                self.assertLess(lit_rows[0], 3)
                self.assertGreater(lit_rows[-1], 44)
                enlarged = np.repeat(np.repeat(frame[::3, ::3], 3, axis=0), 3, axis=1)
                self.assertFalse(np.array_equal(frame, enlarged))
                capture = mock.Mock()
                capture.latest.return_value = self.tone(0.7, 440)
                self.assertEqual(len(next(iter_audio_frames(capture, visualizer))), 4608)

    def test_native_48_palettes_and_preview_keep_their_resolution_after_edits(self):
        controls = PaletteControls(size=48, slowdown=0)
        self.assertEqual(np.array(controls.state()["frame"]).shape, (48, 48, 3))
        for style in ("wave", "spectrum"):
            visualizer = AudioVisualizer(style, controls=controls, size=48)
            for preset in ("rainbow", "moving-rainbow", "adaptive", "ocean", "custom"):
                with self.subTest(style=style, preset=preset):
                    controls.update(default_settings() | {"preset": preset, "slowdown": 0})
                    self.assertEqual(np.array(controls.state()["palette"]).shape, (48, 3))
                    frame = visualizer.render(self.tone(0.7))
                    self.assertEqual(frame.shape, (48, 48, 3))
                    state = controls.state()
                    self.assertEqual(np.array(state["palette"]).shape, (48, 3))
                    np.testing.assert_array_equal(state["frame"], frame)

    def test_native_48_spectrum_has_no_empty_bands_and_reaches_both_edges(self):
        visualizer = self.visualizer("spectrum", size=48)
        self.assertEqual(len(visualizer.band_masks), 48)
        self.assertTrue(all(np.any(mask) for mask in visualizer.band_masks))
        for _ in range(30):
            frame = visualizer.render(self.tone(0.1, SAMPLE_RATE / FFT_SIZE * 5))
        self.assertTrue(np.all(frame[[0, -1]].max(axis=(1, 2)) > 20))

    def test_native_48_wave_fades_to_black(self):
        visualizer = self.visualizer("wave", size=48, slowdown=75)
        visualizer.render(self.tone(0.7))
        for _ in range(120):
            frame = visualizer.render(np.zeros(FFT_SIZE, dtype=np.float32))
        self.assertFalse(np.any(frame))

    def test_native_32_wave_has_individual_pixels_and_full_screen_reach(self):
        for slowdown in (0, 75):
            with self.subTest(slowdown=slowdown):
                visualizer = self.visualizer("wave", size=32, slowdown=slowdown)
                for _ in range(80):
                    frame = visualizer.render(self.tone(0.7, 440))
                self.assertEqual(frame.shape, (32, 32, 3))
                self.assertEqual(frame.dtype, np.uint8)
                self.assertTrue(np.all(np.any(frame, axis=(0, 2))))
                lit_rows = np.flatnonzero(np.any(frame, axis=(1, 2)))
                self.assertLess(lit_rows[0], 3)
                self.assertGreater(lit_rows[-1], 28)
                enlarged = np.repeat(np.repeat(frame[::2, ::2], 2, axis=0), 2, axis=1)
                self.assertFalse(np.array_equal(frame, enlarged))
                capture = mock.Mock()
                capture.latest.return_value = self.tone(0.7, 440)
                self.assertEqual(len(next(iter_audio_frames(capture, visualizer))), 2048)

    def test_native_32_palettes_and_preview_keep_their_resolution_after_edits(self):
        controls = PaletteControls(size=32, slowdown=0)
        self.assertEqual(np.array(controls.state()["frame"]).shape, (32, 32, 3))
        for style in ("wave", "spectrum"):
            visualizer = AudioVisualizer(style, controls=controls, size=32)
            for preset in ("rainbow", "moving-rainbow", "adaptive", "ocean", "custom"):
                with self.subTest(style=style, preset=preset):
                    controls.update(default_settings() | {"preset": preset, "slowdown": 0})
                    self.assertEqual(np.array(controls.state()["palette"]).shape, (32, 3))
                    frame = visualizer.render(self.tone(0.7))
                    self.assertEqual(frame.shape, (32, 32, 3))
                    state = controls.state()
                    self.assertEqual(np.array(state["palette"]).shape, (32, 3))
                    np.testing.assert_array_equal(state["frame"], frame)

    def test_native_32_spectrum_has_no_empty_bands_and_reaches_both_edges(self):
        visualizer = self.visualizer("spectrum", size=32)
        self.assertEqual(len(visualizer.band_masks), 32)
        self.assertTrue(all(np.any(mask) for mask in visualizer.band_masks))
        for _ in range(30):
            frame = visualizer.render(self.tone(0.1, SAMPLE_RATE / FFT_SIZE * 5))
        self.assertTrue(np.all(frame[[0, -1]].max(axis=(1, 2)) > 20))

    def test_native_32_wave_fades_to_black(self):
        visualizer = self.visualizer("wave", size=32, slowdown=75)
        visualizer.render(self.tone(0.7))
        for _ in range(120):
            frame = visualizer.render(np.zeros(FFT_SIZE, dtype=np.float32))
        self.assertFalse(np.any(frame))

    @staticmethod
    def visualizer(style, **kwargs):
        # Keep renderer geometry tests independent of the selected startup look.
        if "controls" not in kwargs:
            controls = PaletteControls(slowdown=kwargs.get("slowdown", 0))
            controls.update(default_settings() | {
                "preset": "rainbow", "brightness": 1,
                "slowdown": kwargs.get("slowdown", 0),
            })
            kwargs["controls"] = controls
        return AudioVisualizer(style, **kwargs)

    @staticmethod
    def tone(amplitude: float, frequency: float = 110.0) -> np.ndarray:
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        return (amplitude * np.sin(2 * np.pi * frequency * time)).astype(
            np.float32
        )

    def test_silence_keeps_the_panel_black(self):
        samples = np.zeros(FFT_SIZE, dtype=np.float32)
        for style, slowdown in (("wave", 0), ("spectrum", 0), ("wave", 75)):
            with self.subTest(style=style, slowdown=slowdown):
                frame = self.visualizer(style, slowdown=slowdown).render(samples)
                self.assertEqual(frame.shape, (GRID_SIZE, GRID_SIZE, 3))
                self.assertFalse(np.any(frame))

    def test_default_db_sensitivity_is_increased_by_fifty_percent(self):
        visualizer = self.visualizer("spectrum")
        args = build_parser().parse_args([])
        normal_frame = self.visualizer("spectrum", sensitivity=1.0).render(
            self.tone(0.02)
        )
        boosted_frame = visualizer.render(self.tone(0.02))

        self.assertEqual(DEFAULT_SENSITIVITY, 1.5)
        self.assertEqual(visualizer.sensitivity, DEFAULT_SENSITIVITY)
        self.assertEqual(args.sensitivity, DEFAULT_SENSITIVITY)
        self.assertGreater(int(boosted_frame.max()), int(normal_frame.max()))

    def test_startup_defaults_and_explicit_rainbow_palette(self):
        settings = default_settings()
        self.assertEqual((settings["preset"], settings["brightness"], settings["slowdown"]),
                         ("custom", 16 / 255, 20))
        self.assertEqual(build_parser().parse_args([]).slowdown, 20)
        for controls in (None, PaletteControls()):
            visualizer = AudioVisualizer("wave", controls=controls)
            visualizer.render(np.zeros(FFT_SIZE))
            self.assertFalse(visualizer._adaptive)
            self.assertEqual(visualizer.slowdown, 20)
            self.assertEqual(visualizer._settings["brightness"], 16 / 255)
        np.testing.assert_array_equal(color_palette(), make_palette(settings))
        palette = make_palette(settings | {"preset": "rainbow", "brightness": 1})
        self.assertEqual(palette.shape, (GRID_SIZE, 3))
        self.assertEqual(palette.dtype, np.uint8)
        np.testing.assert_array_equal(palette[0], [255, 0, 0])
        np.testing.assert_array_equal(palette[-1], [255, 0, 255])
        hues = np.array([
            colorsys.rgb_to_hsv(*(color.astype(float) / 255.0))[0]
            for color in palette
        ])
        np.testing.assert_allclose(hues, np.linspace(0, 5 / 6, GRID_SIZE), atol=0.002)
        self.assertFalse(build_parser().parse_args([]).no_controls)

    def test_default_live_styles_stay_within_the_dim_red_test_level(self):
        for style in ("wave", "spectrum"):
            for controls in (None, PaletteControls(size=32)):
                with self.subTest(style=style, controls=controls is not None):
                    visualizer = AudioVisualizer(style, controls=controls, size=32)
                    for _ in range(80):
                        frame = visualizer.render(self.tone(0.9, 440))
                        self.assertLessEqual(int(frame.max()), 16)
                        self.assertFalse(np.any(frame[:, :, 1:]))
                    self.assertGreater(int(frame.max()), 0)

    def test_live_palette_recolors_both_styles_and_clears_old_trails(self):
        for style, slowdown in (("wave", 0), ("spectrum", 0), ("wave", 75), ("spectrum", 75)):
            with self.subTest(style=style, slowdown=slowdown):
                controls = PaletteControls(slowdown=slowdown)
                visualizer = self.visualizer(style, controls=controls)
                samples = self.tone(0.7, 440)
                for _ in range(8):
                    visualizer.render(samples)
                settings = default_settings() | {"slowdown": slowdown}
                settings.update(preset="custom", colors=["#00ff00"])
                controls.update(settings)
                frame = visualizer.render(samples)
                self.assertTrue(np.any(frame[:, :, 1]))
                self.assertFalse(np.any(frame[:, :, [0, 2]]))
                np.testing.assert_array_equal(controls.state()["frame"], frame)
                settings["brightness"] = 0
                controls.update(settings)
                self.assertFalse(np.any(visualizer.render(samples)))

    def test_spectrum_is_brightest_on_x_axis_and_keeps_full_height_edges_visible(self):
        visualizer = self.visualizer("spectrum")
        for _ in range(10):
            frame = visualizer.render(self.tone(0.7))

        column_brightness = frame.max(axis=(0, 2))
        column = int(np.argmax(column_brightness))
        center = int(frame[7:9, column].max())
        near = int(frame[[5, 10], column].max())
        far = int(frame[[0, 15], column].max())

        self.assertGreater(center, near)
        self.assertGreater(near, far)
        self.assertGreater(far, center * 0.1)

    def test_loud_audio_reaches_top_and_bottom_after_rgb565_encoding(self):
        samples = self.tone(0.7, SAMPLE_RATE / FFT_SIZE * 5)
        for style, slowdown in (("spectrum", 0), ("wave", 0), ("wave", 1), ("wave", 95)):
            with self.subTest(style=style, slowdown=slowdown):
                visualizer = self.visualizer(style, slowdown=slowdown)
                for _ in range(240):
                    frame = visualizer.render(samples)
                encoded = np.frombuffer(encode_rgb565(frame), dtype="<u2").reshape(
                    GRID_SIZE, GRID_SIZE
                )
                self.assertTrue(np.any(encoded[0]), "top LED row should light up")
                self.assertTrue(np.any(encoded[-1]), "bottom LED row should light up")

    def test_middle_frequency_survives_rgb565_output(self):
        visualizer = self.visualizer("spectrum")
        middle_column = GRID_SIZE // 2
        middle_bins = np.flatnonzero(visualizer.band_masks[middle_column])
        middle_bin = int(middle_bins[len(middle_bins) // 2])
        frequency = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)[middle_bin]

        for _ in range(8):
            frame = visualizer.render(self.tone(0.4, float(frequency)))

        encoded = np.frombuffer(encode_rgb565(frame), dtype="<u2").reshape(
            GRID_SIZE, GRID_SIZE
        )
        self.assertTrue(np.any(encoded[:, middle_column]))

    def test_spectrum_responds_to_a_bass_tone(self):
        samples = self.tone(0.5)
        frame = self.visualizer("spectrum").render(samples)
        lit_columns = np.flatnonzero(np.any(frame, axis=(0, 2)))

        self.assertGreater(len(lit_columns), 0)
        self.assertLess(int(lit_columns[0]), GRID_SIZE // 2)

    def test_spectrum_is_bigger_and_brighter_for_louder_audio(self):
        quiet = self.visualizer("spectrum").render(self.tone(0.003))
        loud = self.visualizer("spectrum").render(self.tone(0.7))

        # Count the bright bars above the faint full-panel background.
        quiet_pixels = int(np.count_nonzero(quiet.max(axis=2) > 20))
        loud_pixels = int(np.count_nonzero(loud.max(axis=2) > 20))
        self.assertGreater(loud_pixels, quiet_pixels)
        self.assertGreater(int(loud.max()), int(quiet.max()))

    def test_spectrum_uses_every_led_during_audio_and_fades_to_black_in_silence(self):
        for slowdown in (0, 75):
            with self.subTest(slowdown=slowdown):
                visualizer = self.visualizer("spectrum", slowdown=slowdown)
                frame = visualizer.render(self.tone(0.003))
                encoded = np.frombuffer(encode_rgb565(frame), dtype="<u2")
                self.assertTrue(np.all(encoded != 0))
                for _ in range(240):
                    frame = visualizer.render(np.zeros(FFT_SIZE, dtype=np.float32))
                self.assertFalse(np.any(frame))

    def test_spectrum_bars_reach_full_height_before_audio_is_maxed_out(self):
        visualizer = self.visualizer("spectrum")
        for _ in range(30):
            frame = visualizer.render(self.tone(0.05, SAMPLE_RATE / FFT_SIZE * 5))
        edge_brightness = frame[[0, -1]].max(axis=2)
        self.assertTrue(np.all(edge_brightness.max(axis=1) > 20))

    def test_louder_frequency_band_is_brighter_than_quiet_band(self):
        bass_frequency = SAMPLE_RATE / FFT_SIZE * 5
        high_frequency = SAMPLE_RATE / FFT_SIZE * 80
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (
            0.7 * np.sin(2 * np.pi * bass_frequency * time)
            + 0.02 * np.sin(2 * np.pi * high_frequency * time)
        ).astype(np.float32)
        visualizer = self.visualizer("spectrum")
        for _ in range(8):
            frame = visualizer.render(samples)

        frequencies = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        bass_bin = int(np.argmin(np.abs(frequencies - bass_frequency)))
        high_bin = int(np.argmin(np.abs(frequencies - high_frequency)))
        bass_column = next(
            index for index, mask in enumerate(visualizer.band_masks) if mask[bass_bin]
        )
        high_column = next(
            index for index, mask in enumerate(visualizer.band_masks) if mask[high_bin]
        )

        bass_brightness = int(frame[:, bass_column].max())
        high_brightness = int(frame[:, high_column].max())
        self.assertGreater(bass_brightness, high_brightness * 1.5)

    def test_spectrum_brightness_rises_smoothly(self):
        visualizer = self.visualizer("spectrum")
        quiet = visualizer.render(self.tone(0.003))
        first_loud = visualizer.render(self.tone(0.7))
        for _ in range(10):
            steady_loud = visualizer.render(self.tone(0.7))

        self.assertGreater(int(first_loud.max()), int(quiet.max()))
        self.assertLess(int(first_loud.max()), int(steady_loud.max()))

    def test_loud_notes_fade_at_a_higher_rate(self):
        quiet_visualizer = self.visualizer("spectrum")
        loud_visualizer = self.visualizer("spectrum")
        for _ in range(10):
            quiet_before = quiet_visualizer.render(self.tone(0.02))
            loud_before = loud_visualizer.render(self.tone(0.7))

        silence = np.zeros(FFT_SIZE, dtype=np.float32)
        quiet_after = quiet_visualizer.render(silence)
        loud_after = loud_visualizer.render(silence)

        quiet_remaining = float(quiet_after.max()) / float(quiet_before.max())
        loud_remaining = float(loud_after.max()) / float(loud_before.max())
        self.assertLess(loud_remaining, quiet_remaining)

    def test_wave_is_bigger_and_brighter_for_louder_audio(self):
        quiet = self.visualizer("wave").render(self.tone(0.003, 440.0))
        loud = self.visualizer("wave").render(self.tone(0.7, 440.0))

        quiet_rows = int(np.count_nonzero(np.any(quiet, axis=(1, 2))))
        loud_rows = int(np.count_nonzero(np.any(loud, axis=(1, 2))))
        self.assertGreater(loud_rows, quiet_rows)
        self.assertGreater(int(loud.max()), int(quiet.max()))

    def test_wave_draws_across_all_columns(self):
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (0.4 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
        frame = self.visualizer("wave").render(samples)

        self.assertTrue(np.all(np.any(frame, axis=(0, 2))))
        self.assertGreater(len(np.flatnonzero(np.any(frame, axis=(1, 2)))), 2)

    def test_slow_wave_has_thick_strokes_that_widen_with_slowdown(self):
        samples = self.tone(0.7, 440)
        frames = [self.visualizer("wave", slowdown=s).render(samples) for s in (1, 95)]
        for frame in frames:
            lit_per_column = np.count_nonzero(frame.max(axis=2) > frame.max() * 0.4, axis=0)
            self.assertTrue(np.all(lit_per_column >= 2))
        self.assertGreater(int(frames[1].sum()), int(frames[0].sum()))

    def test_slow_wave_keeps_moving_with_steady_audio_and_slows_with_slider(self):
        samples = self.tone(0.7, 440)
        motion = []
        for slowdown in (50, 95):
            visualizer = self.visualizer("wave", slowdown=slowdown)
            for _ in range(200):
                visualizer.render(samples)
            frames = np.array([visualizer.render(samples) for _ in range(120)], dtype=float)
            motion.append(float(np.abs(np.diff(frames, axis=0)).mean()))
        self.assertGreater(motion[1], 0.0)
        self.assertGreater(motion[0], motion[1] * 3)

    def test_slow_wave_remains_audio_reactive_and_fades_after_audio_stops(self):
        quiet = self.visualizer("wave", slowdown=75).render(self.tone(0.003))
        visualizer = self.visualizer("wave", slowdown=75)
        loud = visualizer.render(self.tone(0.7))
        self.assertGreater(int(loud.max()), int(quiet.max()))
        self.assertGreater(np.count_nonzero(np.any(loud, axis=(1, 2))),
                           np.count_nonzero(np.any(quiet, axis=(1, 2))))
        for _ in range(120):
            faded = visualizer.render(np.zeros(FFT_SIZE, dtype=np.float32))
        self.assertFalse(np.any(faded))


if __name__ == "__main__":
    unittest.main()
