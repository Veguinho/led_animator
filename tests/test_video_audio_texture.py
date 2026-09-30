import unittest

import numpy as np

from audio_palette_controls import PaletteControls, default_settings
from video_audio_texture import LiveAudioVideoTexture
from video_controls import VideoPlaybackControls


class VideoAudioTextureTests(unittest.TestCase):
    def setUp(self):
        self.controls = PaletteControls(size=48)
        self.controls.update(default_settings() | {
            "preset": "adaptive", "brightness": 1.0,
        })
        self.video = VideoPlaybackControls("clip.mp4", 60, 30000 / 1001)
        self.texture = LiveAudioVideoTexture(
            self.controls, self.video, lambda count: np.zeros(count), size=48,
        )
        columns = np.arange(48, dtype=np.uint8)[None, :, None]
        self.frame = np.broadcast_to(80 + columns * 3, (48, 48, 3)).copy()

    def test_music_increases_saturation_and_brightness_without_erasing_texture(self):
        silence = np.zeros(2048, dtype=np.float32)
        phase = np.arange(2048) / 48_000
        music = (0.16 * np.sin(2 * np.pi * 110 * phase)
                 + 0.10 * np.sin(2 * np.pi * 880 * phase)
                 + 0.06 * np.sin(2 * np.pi * 5000 * phase)).astype(np.float32)
        calm = self.texture.render(self.frame, silence, 0.05)
        for _ in range(45):
            energetic = self.texture.render(self.frame, music, 0.05)
        state = self.video.state()["audio_reactive"]
        self.assertTrue(state["audible"])
        self.assertGreater(state["energy"], 0.4)
        self.assertGreater(state["saturation"], 0.5)
        self.assertGreater(float(energetic.mean()), float(calm.mean()))
        self.assertGreater(int(energetic[0, 47].max()), int(energetic[0, 0].max()))
        self.assertFalse(np.array_equal(energetic, self.frame))

    def test_palette_changes_live_and_silence_cools_the_texture(self):
        phase = np.arange(2048) / 48_000
        music = (0.18 * np.sin(2 * np.pi * 220 * phase)
                 + 0.12 * np.sin(2 * np.pi * 1600 * phase)).astype(np.float32)
        for _ in range(35):
            self.texture.render(self.frame, music, 0.05)
        energetic = self.video.state()["audio_reactive"]["energy"]
        self.controls.update(default_settings() | {
            "preset": "ocean", "brightness": 1.0, "saturation": 1.0,
        })
        ocean = self.texture.render(self.frame, music, 0.05)
        self.assertTrue(np.any(ocean[..., 2] != ocean[..., 0]))
        for _ in range(50):
            calm = self.texture.render(self.frame, np.zeros(2048), 0.05)
        self.assertLess(self.video.state()["audio_reactive"]["energy"], energetic)
        self.assertFalse(self.video.state()["audio_reactive"]["audible"])
        self.assertTrue(np.any(calm[..., 2] != calm[..., 0]))


if __name__ == "__main__":
    unittest.main()
