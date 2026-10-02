import unittest

import numpy as np

from audio_palette_controls import PaletteControls, default_settings
from stream_arduino import build_parser
from video_audio_texture import LiveAudioVideoTexture
from video_controls import VideoPlaybackControls


class ImmediateVideoAudioTests(unittest.TestCase):
    def setUp(self):
        self.controls = PaletteControls(size=48)
        self.controls.update(default_settings() | {
            "preset": "adaptive", "brightness": 1.0, "slowdown": 95,
        })
        self.video = VideoPlaybackControls("clip.mp4", 60, 30000 / 1001)
        self.samples = np.zeros(1024, dtype=np.float32)
        self.texture = LiveAudioVideoTexture(
            self.controls, self.video, self.latest, size=48, immediate=True,
        )
        self.frame = np.full((48, 48, 3), 120, dtype=np.uint8)

    def latest(self, count):
        self.assertEqual(count, 1024)
        return self.samples

    @staticmethod
    def tone(frequency, amplitude=0.20):
        phase = np.arange(1024) / 48_000
        return (amplitude * np.sin(2 * np.pi * frequency * phase)).astype(np.float32)

    def test_playback_defaults_to_immediate_with_optional_smooth_response(self):
        self.assertEqual(build_parser().parse_args(["clip.mp4"]).audio_response, "immediate")
        self.assertEqual(build_parser().parse_args(
            ["clip.mp4", "--audio-response", "smooth"],
        ).audio_response, "smooth")

    def test_audio_attack_and_frequency_change_are_immediate_while_overlay_releases_softly(self):
        quiet = self.texture.render(self.frame, self.samples, 0)
        self.samples = self.tone(110)
        loud = self.texture.render(self.frame, self.samples, 0)
        self.assertGreater(self.texture.pulse, 0.9)
        self.assertGreater(self.video.state()["audio_reactive"]["energy"], 0.9)
        self.assertGreater(self.video.content_fps(), 28)
        self.assertGreater(self.texture.band_levels[0], 0.9)
        self.assertFalse(np.array_equal(loud, quiet))

        self.texture.render(self.frame, self.tone(6000), 1 / 30)
        self.assertGreater(self.texture.band_levels[2], 0.9)
        self.assertLess(self.texture.band_levels[0], 0.01)
        silent = self.texture.render(self.frame, np.zeros(1024), 1 / 30)
        self.assertEqual(self.texture.pulse, 0)
        self.assertEqual(self.video.state()["audio_reactive"]["energy"], 0)
        np.testing.assert_array_equal(self.texture.band_levels, 0)
        self.assertEqual(self.video.content_fps(), 10)
        self.assertFalse(np.array_equal(silent, quiet))
        self.assertGreater(self.texture.mapping_envelope[0], 0.9)
        for _ in range(180):
            settled = self.texture.render(self.frame, np.zeros(1024), 1 / 30)
        np.testing.assert_array_equal(settled, quiet)

    def test_video_selection_and_texture_share_the_latest_audio(self):
        rates = []

        def select(frame):
            rates.append(self.video.content_fps())
            return frame

        self.texture.apply(self.frame, select_frame=select)
        self.samples = self.tone(110)
        self.texture.apply(self.frame, select_frame=select)
        self.assertEqual(rates[0], 10)
        self.assertGreater(rates[1], 28)

    def test_sound_outside_the_newest_amplitude_window_does_not_keep_the_glow(self):
        self.samples = self.tone(110)
        self.texture.apply(self.frame)
        self.samples[-256:] = 0
        self.texture.apply(self.frame)
        self.assertEqual(self.texture.pulse, 0)
        np.testing.assert_array_equal(self.texture.band_levels, 0)
        self.assertEqual(self.video.state()["audio_reactive"]["energy"], 0)
        self.assertFalse(self.video.state()["audio_reactive"]["audible"])


if __name__ == "__main__":
    unittest.main()
