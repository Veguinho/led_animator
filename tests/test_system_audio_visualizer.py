import colorsys
import unittest

import numpy as np

from export_arduino import encode_rgb565
from system_audio_visualizer import (
    DEFAULT_SENSITIVITY,
    FFT_SIZE,
    GRID_SIZE,
    SAMPLE_RATE,
    AudioVisualizer,
    build_parser,
    color_palette,
)


class AudioVisualizerTests(unittest.TestCase):
    @staticmethod
    def tone(amplitude: float, frequency: float = 110.0) -> np.ndarray:
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        return (amplitude * np.sin(2 * np.pi * frequency * time)).astype(
            np.float32
        )

    def test_silence_keeps_the_panel_black(self):
        samples = np.zeros(FFT_SIZE, dtype=np.float32)
        for style in ("wave", "spectrum"):
            with self.subTest(style=style):
                frame = AudioVisualizer(style).render(samples)
                self.assertEqual(frame.shape, (GRID_SIZE, GRID_SIZE, 3))
                self.assertFalse(np.any(frame))

    def test_default_db_sensitivity_is_increased_by_fifty_percent(self):
        visualizer = AudioVisualizer("spectrum")
        args = build_parser().parse_args([])
        normal_frame = AudioVisualizer("spectrum", sensitivity=1.0).render(
            self.tone(0.02)
        )
        boosted_frame = visualizer.render(self.tone(0.02))

        self.assertEqual(DEFAULT_SENSITIVITY, 1.5)
        self.assertEqual(visualizer.sensitivity, DEFAULT_SENSITIVITY)
        self.assertEqual(args.sensitivity, DEFAULT_SENSITIVITY)
        self.assertGreater(int(boosted_frame.max()), int(normal_frame.max()))

    def test_palette_is_one_smooth_blue_purple_red_gradient(self):
        palette = color_palette()

        self.assertGreater(int(palette[0, 2]), 150)
        self.assertGreater(int(palette[0, 2]), int(palette[0, 0]) * 5)
        self.assertGreater(int(palette[GRID_SIZE // 2, 0]), 150)
        self.assertLess(int(palette[GRID_SIZE // 2, 1]), 30)
        self.assertGreater(int(palette[GRID_SIZE // 2, 2]), 180)
        self.assertGreater(int(palette[-1, 0]), 240)
        self.assertLess(int(palette[-1, 1]), 30)
        self.assertLess(int(palette[-1, 2]), 30)

        adjacent_steps = np.abs(np.diff(palette.astype(int), axis=0))
        self.assertLess(int(adjacent_steps.max()), 100)
        hues = np.array(
            [
                colorsys.rgb_to_hsv(*(color.astype(float) / 255.0))[0]
                for color in palette
            ]
        )
        unwrapped_hues = np.unwrap(hues * 2.0 * np.pi) / (2.0 * np.pi)
        self.assertTrue(np.all(np.diff(unwrapped_hues) > 0.0))

    def test_spectrum_is_brightest_on_x_axis_and_fades_to_black(self):
        visualizer = AudioVisualizer("spectrum")
        for _ in range(10):
            frame = visualizer.render(self.tone(0.7))

        column_brightness = frame.max(axis=(0, 2))
        column = int(np.argmax(column_brightness))
        center = int(frame[7:9, column].max())
        near = int(frame[[5, 10], column].max())
        far = int(frame[[0, 15], column].max())

        self.assertGreater(center, near)
        self.assertGreater(near, far)
        self.assertEqual(far, 0)

    def test_middle_frequency_survives_rgb565_output(self):
        visualizer = AudioVisualizer("spectrum")
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
        frame = AudioVisualizer("spectrum").render(samples)
        lit_columns = np.flatnonzero(np.any(frame, axis=(0, 2)))

        self.assertGreater(len(lit_columns), 0)
        self.assertLess(int(lit_columns[0]), GRID_SIZE // 2)

    def test_spectrum_is_bigger_and_brighter_for_louder_audio(self):
        quiet = AudioVisualizer("spectrum").render(self.tone(0.003))
        loud = AudioVisualizer("spectrum").render(self.tone(0.7))

        quiet_pixels = int(np.count_nonzero(np.any(quiet, axis=2)))
        loud_pixels = int(np.count_nonzero(np.any(loud, axis=2)))
        self.assertGreater(loud_pixels, quiet_pixels)
        self.assertGreater(int(loud.max()), int(quiet.max()))

    def test_louder_frequency_band_is_brighter_than_quiet_band(self):
        bass_frequency = SAMPLE_RATE / FFT_SIZE * 5
        high_frequency = SAMPLE_RATE / FFT_SIZE * 80
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (
            0.7 * np.sin(2 * np.pi * bass_frequency * time)
            + 0.02 * np.sin(2 * np.pi * high_frequency * time)
        ).astype(np.float32)
        visualizer = AudioVisualizer("spectrum")
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
        visualizer = AudioVisualizer("spectrum")
        quiet = visualizer.render(self.tone(0.003))
        first_loud = visualizer.render(self.tone(0.7))
        for _ in range(10):
            steady_loud = visualizer.render(self.tone(0.7))

        self.assertGreater(int(first_loud.max()), int(quiet.max()))
        self.assertLess(int(first_loud.max()), int(steady_loud.max()))

    def test_loud_notes_fade_at_a_higher_rate(self):
        quiet_visualizer = AudioVisualizer("spectrum")
        loud_visualizer = AudioVisualizer("spectrum")
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
        quiet = AudioVisualizer("wave").render(self.tone(0.003, 440.0))
        loud = AudioVisualizer("wave").render(self.tone(0.7, 440.0))

        quiet_rows = int(np.count_nonzero(np.any(quiet, axis=(1, 2))))
        loud_rows = int(np.count_nonzero(np.any(loud, axis=(1, 2))))
        self.assertGreater(loud_rows, quiet_rows)
        self.assertGreater(int(loud.max()), int(quiet.max()))

    def test_wave_draws_across_all_columns(self):
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (0.4 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
        frame = AudioVisualizer("wave").render(samples)

        self.assertTrue(np.all(np.any(frame, axis=(0, 2))))
        self.assertGreater(len(np.flatnonzero(np.any(frame, axis=(1, 2)))), 2)


if __name__ == "__main__":
    unittest.main()
