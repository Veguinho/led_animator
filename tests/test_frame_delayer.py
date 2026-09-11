import unittest
from unittest import mock

import numpy as np

from audio_palette_controls import PaletteControls, default_settings as startup_settings
from system_audio_visualizer import AudioVisualizer, FFT_SIZE, FrameDelayer, main


def default_settings():
    return startup_settings() | {"preset": "rainbow", "brightness": 1, "slowdown": 0}


class FrameDelayerTests(unittest.TestCase):
    def test_half_speed_inserts_a_color_gradient_without_dropping_output_frames(self):
        frames = [np.full((16, 16, 3), value, dtype=np.uint8) for value in (0, 100, 200)]
        source = mock.Mock(side_effect=frames)
        delayer = FrameDelayer()
        output = [delayer.render(source, 50) for _ in range(5)]
        self.assertEqual([int(frame[0, 0, 0]) for frame in output], [0, 50, 100, 150, 200])
        self.assertEqual(source.call_count, 3)
        self.assertTrue(all(frame.shape == (16, 16, 3) and frame.dtype == np.uint8 for frame in output))

    def test_fractional_percentages_keep_the_requested_rate(self):
        for slowdown, ticks, expected in [(50, 60, 30), (75, 60, 15), (95, 60, 3), (67.5, 400, 130)]:
            with self.subTest(slowdown=slowdown):
                source = mock.Mock(return_value=np.zeros((16, 16, 3), dtype=np.uint8))
                delayer = FrameDelayer()
                delayer.render(source, slowdown)
                source.reset_mock()
                for _ in range(ticks):
                    delayer.render(source, slowdown)
                self.assertEqual(source.call_count, expected)

    def test_live_speed_change_continues_the_transition_and_zero_returns_to_live(self):
        source = mock.Mock(side_effect=[np.array([value], dtype=np.uint8) for value in (0, 100, 200)])
        delayer = FrameDelayer()
        self.assertEqual(delayer.render(source, 50)[0], 0)
        self.assertEqual(delayer.render(source, 50)[0], 50)
        self.assertEqual(delayer.render(source, 75)[0], 75)
        self.assertEqual(source.call_count, 2)
        self.assertEqual(delayer.render(source, 0)[0], 200)

    def test_preview_publishes_blended_frames_and_palette_edits_clear_the_delay(self):
        for style in ("wave", "spectrum"):
            with self.subTest(style=style):
                controls = PaletteControls(slowdown=75)
                controls.update(default_settings() | {"slowdown": 75})
                visualizer = AudioVisualizer(style, controls=controls)
                source = mock.Mock(side_effect=[
                    np.zeros((16, 16, 3), dtype=np.uint8),
                    np.full((16, 16, 3), 200, dtype=np.uint8),
                    np.zeros((16, 16, 3), dtype=np.uint8),
                ])
                with mock.patch.object(visualizer, "_render_frame", source):
                    visualizer.render(np.zeros(FFT_SIZE))
                    blended = visualizer.render(np.zeros(FFT_SIZE))
                    np.testing.assert_array_equal(blended, np.full((16, 16, 3), 50))
                    np.testing.assert_array_equal(controls.state()["frame"], blended)
                    controls.update(default_settings() | {"slowdown": 75, "brightness": 0})
                    self.assertFalse(np.any(visualizer.render(np.zeros(FFT_SIZE))))
                    self.assertEqual(source.call_count, 3)

    def test_speed_only_edit_does_not_reset_palette_or_transition(self):
        controls = PaletteControls(slowdown=50)
        controls.update(default_settings() | {"slowdown": 50})
        visualizer = AudioVisualizer("wave", controls=controls)
        source = mock.Mock(side_effect=[
            np.zeros((16, 16, 3), dtype=np.uint8),
            np.full((16, 16, 3), 100, dtype=np.uint8),
        ])
        with mock.patch.object(visualizer, "_render_frame", source):
            visualizer.render(np.zeros(FFT_SIZE))
            visualizer.render(np.zeros(FFT_SIZE))
            controls.update(default_settings() | {"slowdown": 75})
            frame = visualizer.render(np.zeros(FFT_SIZE))
        self.assertEqual(int(frame[0, 0, 0]), 75)
        self.assertEqual(source.call_count, 2)

    def test_invalid_cli_slowdown_is_rejected_before_capturing_audio(self):
        for value in ("-1", "100", "nan", "inf"):
            with self.subTest(value=value), mock.patch("system_audio_visualizer.AudioCapture") as capture:
                with self.assertRaisesRegex(SystemExit, "slowdown"):
                    main(["--slowdown", value])
                capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
