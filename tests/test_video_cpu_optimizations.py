import base64
import io
import json
from pathlib import Path
import unittest
from unittest import mock
from urllib.request import urlopen

import numpy as np

import led_animator
from audio_palette_controls import PaletteControls, PaletteServer
from system_audio_visualizer import AudioCapture
from audio_energy import AdaptiveAudioEnergy
from stream_arduino import _rgb565_pwm_totals, VIDEO_FIRMWARE_OUTPUT_SCALE
from video_audio_texture import LiveAudioVideoTexture
from video_controls import VideoPlaybackControls


class CpuOptimizationTests(unittest.TestCase):
    def test_pwm_lookup_preserves_firmware_total_for_all_rgb565_colors(self):
        packed = np.arange(65536, dtype=np.uint32)
        red5, green6, blue5 = (packed >> 11) & 31, (packed >> 5) & 63, packed & 31
        channels = ((red5 << 3) | (red5 >> 2), (green6 << 2) | (green6 >> 4),
                    (blue5 << 3) | (blue5 >> 2))
        expected = sum(channel * VIDEO_FIRMWARE_OUTPUT_SCALE // 255 for channel in channels)
        np.testing.assert_array_equal(_rgb565_pwm_totals(), expected)

    def test_energy_maxima_match_full_history_across_expiration_and_reset(self):
        energy = AdaptiveAudioEnergy(fft_size=128)
        rng = np.random.default_rng(5)
        for i in range(140):
            elapsed = 31 if i == 70 else 0.4
            samples = rng.normal(0, 0.01 + (i % 15)*0.02, 128).astype(np.float32)
            if i % 17 == 0:
                samples.fill(0)
            energy.update(samples, elapsed, immediate=True)
            if energy.history:
                self.assertEqual(energy._rms_maxima[0][1], max(row[1] for row in energy.history))
                self.assertEqual(energy._peak_maxima[0][1], max(row[2] for row in energy.history))
        energy.reset()
        self.assertFalse(energy._rms_maxima)
        self.assertFalse(energy._peak_maxima)

    def test_compact_preview_is_lossless_and_revision_tracks_published_frames(self):
        controls = PaletteControls(size=48)
        server = PaletteServer(controls, 0)
        server.start()
        self.addCleanup(server.close)
        expected = np.random.default_rng(9).integers(0, 256, (48, 48, 3), dtype=np.uint8)
        controls.publish_frame(expected)
        with urlopen(server.url + "/api/state?compact=1", timeout=2) as response:
            compact = json.load(response)
        actual = np.frombuffer(base64.b64decode(compact["frame_rgb"]), dtype=np.uint8)
        np.testing.assert_array_equal(actual.reshape(48, 48, 3), expected)
        self.assertNotIn("frame", compact)
        self.assertEqual(compact["frame_revision"], 1)
        self.assertEqual(controls.state(compact=True)["frame_rgb"], compact["frame_rgb"])
        self.assertEqual(controls.state(compact=True)["frame_revision"], compact["frame_revision"])
        with urlopen(server.url + "/api/state", timeout=2) as response:
            legacy = json.load(response)
        np.testing.assert_array_equal(legacy["frame"], expected)
        expected.fill(0)
        controls.publish_frame(expected)
        self.assertEqual(controls.state(compact=True)["frame_revision"], 2)
        self.assertNotEqual(controls.state(compact=True)["frame_rgb"], compact["frame_rgb"])

    def test_ring_buffer_matches_sample_history_through_wrap_and_oversized_blocks(self):
        capture = AudioCapture(capacity=11)
        history = np.empty(0, dtype=np.float32)
        for length in (3, 0, 6, 4, 11, 2, 30, 5):
            samples = np.arange(length, dtype=np.float32) + len(history) * 100
            capture._append(samples)
            history = np.concatenate([history, samples])
            for count in (1, 5, 11, 16):
                expected = np.zeros(count, dtype=np.float32)
                tail = history[-min(count, 11):]
                if len(tail):
                    expected[-len(tail):] = tail
                np.testing.assert_array_equal(capture.latest(count), expected)
                returned = capture.latest(count)
                returned.fill(-100)
                np.testing.assert_array_equal(capture.latest(count), expected)

    def test_radial_transport_matches_per_pixel_interpolation(self):
        controls = PaletteControls(size=48)
        video = VideoPlaybackControls("clip", 60, 30)
        texture = LiveAudioVideoTexture(controls, video, lambda n: np.zeros(n), size=48)
        rng = np.random.default_rng(4)
        for elapsed in (0, 1/30, 1/30, 0.07, 0.4, 1.5):
            actual = texture._mapping_fields(rng.random(5), elapsed, 1/30)
            times = np.array([entry[0] for entry in texture.mapping_history])
            values = np.stack([entry[1] for entry in texture.mapping_history])
            travel_time = 0.70 + 0.70 * texture.settings["slowdown"] / 95.0
            delay = travel_time * texture.travel_position
            local_time = texture.mapping_clock - delay
            feather = np.minimum(0.045, delay * 0.5)
            expected = np.stack([
                0.25*np.interp(local_time-feather, times, values[:, channel], left=0.0)
                + 0.50*np.interp(local_time, times, values[:, channel], left=0.0)
                + 0.25*np.interp(local_time+feather, times, values[:, channel], left=0.0)
                for channel in range(5)
            ], axis=-1)
            np.testing.assert_array_equal(actual, expected)

    @staticmethod
    def decoder_process(data, return_code=0):
        process = mock.Mock()
        process.stdout = io.BytesIO(data)
        process.stderr = io.BytesIO(b"unsupported hardware" if return_code else b"")
        process.wait.return_value = return_code
        process.poll.return_value = return_code
        return process

    def test_hardware_failure_before_first_frame_falls_back_to_software(self):
        failed = self.decoder_process(b"", 1)
        good = self.decoder_process(bytes(range(12)))
        with mock.patch.object(led_animator.sys, "platform", "darwin"), \
             mock.patch.object(led_animator.subprocess, "Popen", side_effect=[failed, good]) as launch:
            result = list(led_animator.iter_square_video_frames(
                Path("clip.mp4"), led_animator.VideoInfo(2, 2, 30), 2,
            ))
        self.assertEqual(len(result), 1)
        self.assertIn("-hwaccel", launch.call_args_list[0].args[0])
        self.assertNotIn("-hwaccel", launch.call_args_list[1].args[0])

    def test_hardware_failure_after_first_frame_never_restarts_timeline(self):
        partial = self.decoder_process(bytes(range(12)), 1)
        with mock.patch.object(led_animator.sys, "platform", "darwin"), \
             mock.patch.object(led_animator.subprocess, "Popen", return_value=partial) as launch:
            frames = led_animator.iter_square_video_frames(
                Path("clip.mp4"), led_animator.VideoInfo(2, 2, 30), 2,
            )
            next(frames)
            with self.assertRaisesRegex(RuntimeError, "could not decode"):
                next(frames)
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
