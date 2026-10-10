import unittest

import numpy as np

from audio_palette_controls import PaletteControls, default_settings
from melody_fft import MelodyFFT
from stream_arduino import VideoFrameEncoder, decode_rgb565_frame
from video_audio_texture import LiveAudioVideoTexture
from video_controls import VideoPlaybackControls


def tone(frequency, amplitude=0.08, *, count=4096, start=0):
    time = (np.arange(count) + start) / 48_000
    return amplitude * np.sin(2 * np.pi * frequency * time)


class MelodyFFTTests(unittest.TestCase):
    def test_tracks_melody_over_louder_bass_and_filters_unrelated_frequencies(self):
        tracker = MelodyFFT()
        samples = tone(90, 0.24) + tone(660, 0.06) + tone(6000, 0.12)
        tracker.update(samples, 1 / 30)
        self.assertAlmostEqual(tracker.frequency, 660, delta=2)
        self.assertGreater(tracker.strength, 0.8)
        filtered = np.abs(tracker.filtered_spectrum)
        self.assertEqual(filtered[tracker.frequencies < 180].max(), 0)
        self.assertLess(filtered[tracker.frequencies > 4000].max(), filtered.max() * .001)

    def test_harmonic_family_favors_fundamental_over_louder_second_harmonic(self):
        tracker = MelodyFFT()
        tracker.update(tone(440, .04) + tone(880, .12) + tone(1320, .06), 1 / 30)
        self.assertAlmostEqual(tracker.frequency, 440, delta=2)
        self.assertGreater(abs(tracker.harmonics[1]), abs(tracker.harmonics[0]))
        self.assertGreater(abs(tracker.harmonics[2]), .1)

    def test_follows_note_changes_without_waiting_for_old_salience_to_decay(self):
        tracker = MelodyFFT()
        for _ in range(20):
            tracker.update(tone(440), 1 / 30)
        tracker.update(tone(659.25), 1 / 30)
        self.assertAlmostEqual(tracker.frequency, 659.25, delta=2)

    def test_prefers_current_note_when_two_tones_nearly_tie(self):
        tracker = MelodyFFT()
        tracker.update(tone(440, .08), 1 / 30)
        for index in range(12):
            samples = tone(440, .08) + tone(700, .08 + .003 * (-1) ** index)
            tracker.update(samples, 1 / 30)
            self.assertAlmostEqual(tracker.frequency, 440, delta=2)

    def test_rejects_bass_treble_broadband_noise_and_clicks(self):
        rng = np.random.default_rng(3)
        click = np.zeros(4096)
        click[2048] = 1
        signals = [tone(90, .2), tone(6000, .2), click]
        signals.extend(rng.normal(0, .1, 4096) for _ in range(8))
        for samples in signals:
            with self.subTest(peak=float(np.max(samples))):
                tracker = MelodyFFT()
                tracker.update(samples, 1 / 30)
                self.assertEqual(tracker.strength, 0)
                self.assertEqual(tracker.frequency, 0)

    def test_handles_short_capture_buffers_and_silence_without_stale_notes(self):
        tracker = MelodyFFT()
        tracker.update(tone(523.25, count=1024), 1 / 30)
        self.assertAlmostEqual(tracker.frequency, 523.25, delta=3)
        initial = tracker.strength
        # A stale FFT window must release when the current amplitude is silent.
        tracker.update(tone(523.25), 1 / 30, audible=False)
        self.assertLess(tracker.strength, initial)
        self.assertFalse(np.any(tracker.filtered_spectrum))
        for _ in range(30):
            tracker.update(np.empty(0), 1 / 30)
        self.assertEqual(tracker.frequency, 0)
        self.assertEqual(tracker.strength, 0)
        self.assertFalse(np.any(tracker.harmonics))

    def test_contour_is_closed_and_anchored_to_note_instead_of_capture_phase(self):
        contours = []
        angles = np.linspace(-np.pi, np.pi, 129)
        for start in (0, 1600, 3200):
            tracker = MelodyFFT()
            tracker.update(tone(440, start=start), 1 / 30)
            contour = tracker.contour(angles, .4)
            self.assertAlmostEqual(contour[0], contour[-1], places=10)
            self.assertLessEqual(np.max(np.abs(contour)), 1)
            contours.append(contour)
        np.testing.assert_allclose(contours[0], contours[1], atol=.01)
        np.testing.assert_allclose(contours[0], contours[2], atol=.01)


class MelodyStringTests(unittest.TestCase):
    def make_texture(self, size=48):
        controls = PaletteControls(size=size)
        controls.update(default_settings() | {"preset": "adaptive", "brightness": 1.0})
        video = VideoPlaybackControls("clip.mp4", 60, 30)
        texture = LiveAudioVideoTexture(controls, video, lambda n: np.zeros(n), size=size)
        return controls, video, texture

    def test_string_is_a_thin_closed_foreground_loop_at_every_panel_size(self):
        for size in (16, 32, 48):
            with self.subTest(size=size):
                _, _, texture = self.make_texture(size)
                frame = np.zeros((size, size, 3), dtype=np.uint8)
                texture.render(frame, tone(440), 1 / 30)
                string = texture._melody_string().max(axis=2)
                self.assertGreater(string.max(), 150)
                self.assertLess(np.count_nonzero(string > 80), size * size * .3)
                self.assertLess(string[texture.radius > size * .44].max(), 5)
                self.assertLess(string[texture.radius < size * .12].max(), 5)
                # Every angular sector contains a visible piece of the loop.
                for angle in np.linspace(-np.pi, np.pi, 9)[:-1]:
                    sector = np.abs(np.angle(np.exp(1j * (texture.angle - angle)))) < np.pi / 8
                    self.assertGreater(string[sector].max(), 80)

    def test_string_pulses_with_filtered_melody_amplitude(self):
        strengths = []
        for amplitude in (.005, .05):
            _, _, texture = self.make_texture()
            texture.render(np.zeros((48, 48, 3), dtype=np.uint8),
                           tone(90, .24) + tone(660, amplitude), 1 / 30)
            strengths.append(texture.melody.strength)
        self.assertGreater(strengths[1], strengths[0] + .5)

    def test_pitch_changes_move_the_string_radius(self):
        radii = []
        for frequency in (220, 1760):
            _, _, texture = self.make_texture()
            texture.render(np.zeros((48, 48, 3), dtype=np.uint8), tone(frequency), 1 / 30)
            string = texture._melody_string().max(axis=2)
            radii.append(float(np.sum(string * texture.radius) / string.sum()))
        self.assertGreater(radii[1], radii[0] + 1)

    def test_audio_switch_removes_string_and_brightness_zero_blacks_out_audio(self):
        controls, video, texture = self.make_texture()
        frame = np.full((48, 48, 3), 60, dtype=np.uint8)
        texture.render(frame, tone(440), 1 / 30)
        self.assertGreater(texture.melody.strength, .5)
        video.set_layers({"audio_palette": False})
        np.testing.assert_array_equal(texture.render(frame, tone(440), 1 / 30), frame)
        self.assertEqual(texture.melody.strength, 0)
        video.set_layers({"audio_palette": True})
        controls.update(controls.settings_snapshot()[1] | {"brightness": 0.0})
        np.testing.assert_array_equal(texture.render(frame, tone(440), 1 / 30), frame)
        video.set_layers({"video": False})
        np.testing.assert_array_equal(texture.render(frame, tone(440), 1 / 30), 0)

    def test_string_reaches_encoded_led_output_with_video_paused_and_hidden(self):
        controls, video, texture = self.make_texture()
        video.set_paused(True)
        video.set_layers({"video": False})
        requested = []

        def latest(count):
            requested.append(count)
            return tone(440, count=count)

        texture.latest_samples = latest
        encoder = VideoFrameEncoder(30, 1, 48, None, texture)
        payload = encoder.prepare(np.zeros((48, 48, 3), dtype=np.uint8).tobytes())
        encoder.publish(payload)
        decoded = decode_rgb565_frame(payload, 48)
        np.testing.assert_array_equal(controls.state()["frame"], decoded)
        self.assertEqual(requested, [4096])
        self.assertGreater(decoded.max(), 80)
        self.assertTrue(video.is_paused())


if __name__ == "__main__":
    unittest.main()
