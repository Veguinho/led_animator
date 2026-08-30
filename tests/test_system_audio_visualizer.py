import unittest

import numpy as np

from system_audio_visualizer import (
    FFT_SIZE,
    GRID_SIZE,
    SAMPLE_RATE,
    AudioVisualizer,
)


class AudioVisualizerTests(unittest.TestCase):
    def test_silence_keeps_the_panel_black(self):
        samples = np.zeros(FFT_SIZE, dtype=np.float32)
        for style in ("wave", "spectrum"):
            with self.subTest(style=style):
                frame = AudioVisualizer(style).render(samples)
                self.assertEqual(frame.shape, (GRID_SIZE, GRID_SIZE, 3))
                self.assertFalse(np.any(frame))

    def test_spectrum_responds_to_a_bass_tone(self):
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (0.5 * np.sin(2 * np.pi * 110 * time)).astype(np.float32)
        frame = AudioVisualizer("spectrum").render(samples)
        lit_columns = np.flatnonzero(np.any(frame, axis=(0, 2)))

        self.assertGreater(len(lit_columns), 0)
        self.assertLess(int(lit_columns[0]), GRID_SIZE // 2)

    def test_wave_draws_across_all_columns(self):
        time = np.arange(FFT_SIZE) / SAMPLE_RATE
        samples = (0.4 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
        frame = AudioVisualizer("wave").render(samples)

        self.assertTrue(np.all(np.any(frame, axis=(0, 2))))
        self.assertGreater(len(np.flatnonzero(np.any(frame, axis=(1, 2)))), 2)


if __name__ == "__main__":
    unittest.main()
