import unittest

import numpy as np

from audio_palette_controls import PaletteControls, default_settings
from stream_arduino import VideoFrameEncoder, decode_rgb565_frame
from video_audio_texture import (
    LiveAudioVideoTexture, compose_output_layers, make_video_energy_palette,
)
from video_controls import VideoPlaybackControls


class VideoAudioTextureTests(unittest.TestCase):
    def test_adaptive_video_colors_pass_through_cool_middle_and_warm_stages(self):
        quiet = make_video_energy_palette(0)
        middle = make_video_energy_palette(0.5)
        energetic = make_video_energy_palette(1)
        self.assertGreater(float(quiet[:, 2].mean()), float(quiet[:, 0].mean()) * 2)
        self.assertLess(int(quiet.max()), 100)
        self.assertGreater(int(middle[0, 0]), int(middle[0, 1]) * 2)
        self.assertGreater(int(middle[-1, 1]), int(middle[-1, 0]) * 2)
        self.assertTrue(np.all(energetic[:, 0] > energetic[:, 2] * 2))
        self.assertGreater(float(energetic[:, 1].mean()), float(quiet[:, 1].mean()) * 4)
        self.assertGreater(float(energetic.mean()), float(middle.mean()))
        self.assertGreater(float(middle.mean()), float(quiet.mean()))
        np.testing.assert_array_equal(
            make_video_energy_palette(0.5, reverse=True), middle[::-1],
        )
        np.testing.assert_array_equal(make_video_energy_palette(0.1), quiet)
        np.testing.assert_array_equal(make_video_energy_palette(0.8), energetic)

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

    def test_turning_mapping_off_streams_original_frames_at_full_speed(self):
        encoder = VideoFrameEncoder(self.video.fps, 1, 48, None, self.texture)
        raw_encoder = VideoFrameEncoder(self.video.fps, 1, 48, None, None)
        encoder.prepare(self.frame.tobytes())
        self.assertLess(self.video.content_fps(), self.video.fps)
        self.video.set_texture_mapping(False)
        self.controls.update(default_settings() | {"brightness": 0.01, "saturation": 0})
        for value in (30, 90, 150):
            frame = np.full((48, 48, 3), [value, 40, 200], dtype=np.uint8)
            with self.subTest(value=value):
                payload = encoder.prepare(frame.tobytes())
                self.assertEqual(payload, raw_encoder.prepare(frame.tobytes()))
                self.assertEqual(self.video.content_fps(), self.video.fps)
                encoder.publish(payload)
                np.testing.assert_array_equal(
                    self.controls.state()["frame"], decode_rgb565_frame(payload, 48),
                )
        self.video.set_texture_mapping(True)
        # Use a visible palette for this encoding check: the translucent
        # overlay at 1% brightness can fall below RGB565 quantization.
        self.controls.update(self.controls.settings_snapshot()[1] | {
            "brightness": 1.0, "saturation": 1.0,
        })
        self.assertNotEqual(encoder.prepare(frame.tobytes()), raw_encoder.prepare(frame.tobytes()))
        self.assertLess(self.video.content_fps(), self.video.fps)

    def test_all_four_output_layer_combinations(self):
        silence = np.zeros(1024)
        self.video.set_layers({"video": False, "audio_palette": True})
        palette_only = self.texture.render(self.frame, silence, 0)
        self.assertTrue(np.any(palette_only))
        # Hiding video makes the audio layer independent of its image.
        np.testing.assert_array_equal(
            self.texture.render(np.zeros_like(self.frame), silence, 0), palette_only,
        )
        np.testing.assert_array_equal(
            self.texture.render(255 - self.frame, silence, 0), palette_only,
        )
        self.video.set_layers({"video": True})
        combined = self.texture.render(self.frame, silence, 0)
        np.testing.assert_array_equal(combined, compose_output_layers(
            self.frame, palette_only, {"video": True, "audio_palette": True},
        ))
        self.assertFalse(np.array_equal(combined, self.frame))
        self.assertFalse(np.array_equal(combined, palette_only))
        self.video.set_layers({"audio_palette": False})
        np.testing.assert_array_equal(
            self.texture.render(self.frame, silence, 0), self.frame,
        )
        self.video.set_layers({"video": False})
        np.testing.assert_array_equal(self.texture.render(self.frame, silence, 0), 0)
        self.video.set_layers({"audio_palette": True})
        np.testing.assert_array_equal(
            self.texture.render(self.frame, silence, 0), palette_only,
        )

    def test_overlay_is_translucent_and_standalone_palette_keeps_its_brightness(self):
        video = np.full((48, 48, 3), 100, dtype=np.uint8)
        palette = np.full_like(video, 200)
        blended = compose_output_layers(
            video, palette, {"video": True, "audio_palette": True},
        )
        # Full-strength screening would raise this video to 222. The overlay
        # now contributes less than half of that lift, preserving its detail.
        self.assertGreater(int(blended[0, 0, 0]), 100)
        self.assertLess(int(blended[0, 0, 0]), 161)
        np.testing.assert_array_equal(compose_output_layers(
            video, palette, {"video": False, "audio_palette": True},
        ), palette)

    def test_live_waveform_deforms_only_the_top_and_settles_in_silence(self):
        self.controls.update(default_settings() | {
            "preset": "ocean", "brightness": 1.0, "slowdown": 95,
        })
        texture = LiveAudioVideoTexture(
            self.controls, self.video, lambda count: np.zeros(count),
            size=48, immediate=True,
        )
        phase = np.arange(1024) / 48_000
        bass = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        quiet = texture.render(self.frame, np.zeros(1024), 0)
        positive = texture.render(self.frame, bass, 0)
        negative = texture.render(self.frame, -bass, 0)
        # Identical spectrum and loudness, opposite waveform: only the upper
        # contour changes, even when the travelling wave clock is stationary.
        self.assertFalse(np.array_equal(positive[:24], negative[:24]))
        np.testing.assert_array_equal(positive[24:], negative[24:])
        texture.render(self.frame, np.zeros(1024), 0)
        np.testing.assert_array_equal(texture._top_wave_radius(np.zeros(1024), 0), texture.radius)
        for _ in range(180):
            settled = texture.render(self.frame, np.zeros(1024), 1 / 30)
        np.testing.assert_array_equal(settled, quiet)
        old_sound = bass.copy()
        old_sound[-256:] = 0
        np.testing.assert_array_equal(texture.render(self.frame, old_sound, 0), quiet)

    def test_first_note_changes_the_center_before_the_outer_overlay(self):
        flat = np.full_like(self.frame, 100)
        phase = np.arange(1024) / 48_000
        bass = (0.2 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        quiet = self.texture.render(flat, np.zeros(1024), 0)
        attack = self.texture.render(flat, bass, 0)
        # Glow responds on the first note while its hue stays cool and fades
        # toward warm colors over subsequent frames.
        self.assertGreater(float(np.abs(attack[22:26, 22:26].astype(float)
                                       - quiet[22:26, 22:26]).mean()), 3)
        self.assertGreater(int(attack[24, 24, 2]), int(attack[24, 24, 0]))
        np.testing.assert_array_equal(attack[40:], quiet[40:])
        changes = []
        for _ in range(27):
            expanded = self.texture.render(flat, bass, 1 / 30)
            changes.append(float(np.abs(expanded[40:].astype(float) - quiet[40:]).mean()))
        self.assertGreater(changes[-1], 10)
        self.assertGreater(changes[-1], changes[0] + 10)

    def test_transparency_front_reaches_successive_rings_and_decelerates(self):
        phase = np.arange(1024) / 48_000
        bass = (0.2 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        self.texture.render(self.frame, np.zeros(1024), 0)
        self.texture.render(self.frame, bass, 0)
        rings = [(np.abs(self.texture.radius / self.texture.max_radius - radius) < 0.025)
                 for radius in (0.25, 0.5, 0.75)]
        arrivals = [None, None, None]
        for tick in range(1, 80):
            self.texture.render(self.frame, bass, 1 / 60)
            fields = self.texture._mapping_fields(
                self.texture.mapping_envelope.copy(), 0, 0,
            )
            for index, ring in enumerate(rings):
                if arrivals[index] is None and fields[..., 1][ring].mean() > 0.5:
                    arrivals[index] = tick / 60
        self.assertTrue(all(arrival is not None for arrival in arrivals))
        self.assertLess(arrivals[0], arrivals[1])
        self.assertLess(arrivals[1], arrivals[2])
        self.assertGreater(arrivals[2] - arrivals[1], arrivals[1] - arrivals[0])

    def test_strong_bass_gaps_do_not_reset_color_and_transparency(self):
        flat = np.full_like(self.frame, 100)
        frames = []
        energies = []
        for tick in range(120):
            phase = (np.arange(1024) + tick * 1600) / 48_000
            samples = (0.2 * np.sin(2 * np.pi * 55 * phase)).astype(np.float32)
            if tick % 2:
                samples.fill(0)
            frames.append(self.texture.render(flat, samples, 1 / 30).astype(float))
            energies.append(float(self.texture.mapping_envelope[0]))
        changes = [np.abs(after - before).mean()
                   for before, after in zip(frames[60:], frames[61:])]
        self.assertGreater(min(energies[60:]), 0.9)
        self.assertLess(max(changes), 3)

    def test_opacity_can_expand_per_pixel_and_keeps_palette_only_output(self):
        source = np.full_like(self.frame, 100)
        palette = np.full_like(self.frame, 200)
        opacity = np.zeros((48, 48), dtype=np.float32)
        opacity[22:26, 22:26] = 0.45
        blended = compose_output_layers(source, palette,
            {"video": True, "audio_palette": True}, opacity=opacity)
        np.testing.assert_array_equal(blended[0], source[0])
        self.assertGreater(int(blended[24, 24, 0]), 100)
        np.testing.assert_array_equal(compose_output_layers(source, palette,
            {"video": False, "audio_palette": True}, opacity=opacity), palette)

    def test_layer_toggles_reach_encoded_and_published_led_output(self):
        encoder = VideoFrameEncoder(self.video.fps, 1, 48, None, self.texture)
        self.video.set_layers({"video": False, "audio_palette": False})
        black = encoder.prepare(self.frame.tobytes())
        self.assertEqual(black, bytes(48 * 48 * 2))
        encoder.publish(black)
        np.testing.assert_array_equal(self.controls.state()["frame"], 0)
        self.video.set_layers({"audio_palette": True})
        palette = encoder.prepare(self.frame.tobytes())
        self.assertNotEqual(palette, black)
        encoder.publish(palette)
        np.testing.assert_array_equal(
            self.controls.state()["frame"], decode_rgb565_frame(palette, 48),
        )

    def test_video_layer_can_be_hidden_without_audio_capture(self):
        texture = LiveAudioVideoTexture(self.controls, self.video, None, size=48)
        np.testing.assert_array_equal(texture.apply(self.frame), self.frame)
        self.assertFalse(self.video.state()["audio_reactive"]["enabled"])
        self.video.set_layers({"video": False})
        np.testing.assert_array_equal(texture.apply(self.frame), 0)

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

    def test_palette_forms_concentric_colors_in_silence(self):
        self.controls.update(default_settings() | {
            "preset": "ocean", "brightness": 1.0,
        })
        flat = np.full((48, 48, 3), 180, dtype=np.uint8)
        result = self.texture.render(flat, np.zeros(2048), 0.05)
        np.testing.assert_array_equal(result[10, 24], result[24, 10])
        self.assertFalse(np.array_equal(result[24, 24], result[0, 0]))

    def test_music_moves_the_audio_layer_over_unchanged_video_pixels(self):
        stripes = np.full((48, 48, 3), 200, dtype=np.uint8)
        stripes[:, 15] = 0
        silence = np.zeros(2048, dtype=np.float32)
        quiet = self.texture.render(stripes, silence, 0.05)
        self.assertTrue(np.any(quiet[:, 15] > 0))
        self.video.set_layers({"audio_palette": False})
        np.testing.assert_array_equal(
            self.texture.render(stripes, silence, 0.05), stripes,
        )
        self.video.set_layers({"audio_palette": True})

        phase = np.arange(2048) / 48_000
        music = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        for _ in range(25):
            moving = self.texture.render(stripes, music, 0.05)
        self.assertTrue(np.any(moving[:, 15].max(axis=1) > 20))
        later = self.texture.render(stripes, music, 0.05)
        self.assertFalse(np.array_equal(moving, later))

    def test_reflected_wave_reacts_immediately_but_travels_slowly(self):
        phase = np.arange(2048) / 48_000
        music = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        self.texture.render(self.frame, music, 0.05)
        self.assertGreater(self.texture.pulse, 0)
        self.assertGreater(self.texture.pulse, 0.9)
        for _ in range(24):
            self.texture.render(self.frame, music, 0.05)
        self.assertGreater(self.texture.wave_phase, 0)
        self.assertLess(self.texture.wave_phase, np.pi / 2)

        slow_controls = PaletteControls(size=48)
        slow_controls.update(default_settings() | {
            "preset": "adaptive", "brightness": 1.0, "slowdown": 95,
        })
        slow_texture = LiveAudioVideoTexture(
            slow_controls, self.video, lambda count: np.zeros(count), size=48,
        )
        for _ in range(25):
            slow_texture.render(self.frame, music, 0.05)
        self.assertLess(slow_texture.wave_phase, self.texture.wave_phase / 2)

    def test_beats_and_new_frequencies_attack_in_one_frame_with_a_short_release(self):
        phase = np.arange(1024) / 48_000
        bass = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        highs = (0.20 * np.sin(2 * np.pi * 6000 * phase)).astype(np.float32)
        self.controls.update(self.controls.settings_snapshot()[1] | {"slowdown": 95})
        quiet = self.texture.render(self.frame, np.zeros(1024), 0.0)
        loud = self.texture.render(self.frame, bass, 0.0)
        self.assertGreater(self.texture.pulse, 0.9)
        self.assertGreater(self.video.state()["audio_reactive"]["energy"], 0.9)
        self.assertGreater(self.video.content_fps(), 28)
        self.assertGreater(self.texture.band_levels[0], 0.9)
        self.assertFalse(np.array_equal(loud, quiet))
        self.texture.render(self.frame, highs, 1 / 30)
        self.assertGreater(self.texture.band_levels[2], 0.9)
        self.assertLess(self.texture.band_levels[0], 0.65)
        self.texture.render(self.frame, np.zeros(1024), 1 / 30)
        self.assertFalse(self.video.state()["audio_reactive"]["audible"])
        self.assertGreater(self.texture.pulse, 0)
        self.assertLess(self.texture.pulse, 0.6)
        for _ in range(7):
            self.texture.render(self.frame, np.zeros(1024), 1 / 30)
        self.assertLess(self.texture.pulse, 0.01)
        self.assertLess(self.video.state()["audio_reactive"]["energy"], 0.06)
        self.assertLess(float(self.texture.band_levels.max()), 0.02)
        for _ in range(8):
            self.texture.render(self.frame, np.zeros(1024), 1 / 30)
        self.assertEqual(self.texture.pulse, 0)
        self.assertEqual(self.video.state()["audio_reactive"]["energy"], 0)
        np.testing.assert_array_equal(self.texture.band_levels, 0)
        self.assertEqual(self.video.content_fps(), 10)

    def test_adaptive_colors_fade_on_attack_and_release_in_both_response_modes(self):
        silence = np.zeros(1024, dtype=np.float32)
        phase = np.arange(1024) / 48_000
        bass = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        self.video.set_layers({"video": False})
        for immediate in (False, True):
            with self.subTest(immediate=immediate):
                texture = LiveAudioVideoTexture(
                    self.controls, self.video, lambda count: silence,
                    size=48, immediate=immediate,
                )
                quiet = texture.render(self.frame, silence, 0)
                previous = texture.palette.astype(float)
                first = texture.render(self.frame, bass, 1 / 30)
                self.assertGreater(texture.pulse, 0.9)
                self.assertGreater(int(first[24, 24, 2]), int(first[24, 24, 0]))
                self.assertLess(np.abs(texture.palette.astype(float) - previous).max(), 20)
                previous = texture.palette.astype(float)
                for signal, duration, target in ((bass, 180, 1), (silence, 240, 0)):
                    for _ in range(duration):
                        result = texture.render(self.frame, signal, 1 / 30)
                        palette = texture.palette.astype(float)
                        self.assertLess(np.abs(palette - previous).max(), 20)
                        previous = palette
                    np.testing.assert_allclose(
                        texture.palette, make_video_energy_palette(target), atol=1,
                    )
                np.testing.assert_allclose(result, quiet, atol=1)

    def test_manual_color_edits_crossfade_through_intermediate_colors(self):
        self.video.set_layers({"video": False})
        silence = np.zeros(1024, dtype=np.float32)
        settings = default_settings() | {
            "preset": "custom", "colors": ["#ff0000"], "brightness": 1.0,
        }
        self.controls.update(settings)
        red = self.texture.render(self.frame, silence, 0)
        self.controls.update(settings | {"colors": ["#0000ff"]})
        np.testing.assert_array_equal(self.texture.render(self.frame, silence, 0), red)
        previous = red.astype(float)
        for tick in range(180):
            blended = self.texture.render(self.frame, silence, 1 / 30)
            self.assertLess(np.abs(blended.astype(float) - previous).max(), 10)
            previous = blended.astype(float)
            if tick == 9:
                self.assertGreater(int(self.texture.palette[0, 0]), 0)
                self.assertGreater(int(self.texture.palette[0, 2]), 0)
        blue_texture = LiveAudioVideoTexture(
            self.controls, self.video, lambda count: silence, size=48,
        )
        blue = blue_texture.render(self.frame, silence, 0)
        np.testing.assert_allclose(blended, blue, atol=1)

    def test_color_fade_uses_elapsed_time_and_slowdown(self):
        silence = np.zeros(1024)
        phase = np.arange(1024) / 48_000
        bass = (0.2 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        palettes = []
        for fps, slowdown in ((30, 20), (60, 20), (30, 95)):
            controls = PaletteControls(size=48)
            controls.update(default_settings() | {
                "preset": "adaptive", "brightness": 1.0, "slowdown": slowdown,
            })
            texture = LiveAudioVideoTexture(
                controls, self.video, lambda count: silence, size=48, immediate=True,
            )
            texture.render(self.frame, silence, 0)
            for _ in range(fps):
                texture.render(self.frame, bass, 1 / fps)
            palettes.append(texture.palette.astype(float))
        np.testing.assert_allclose(palettes[0], palettes[1], atol=1)
        warm = make_video_energy_palette(1).astype(float)
        self.assertGreater(np.abs(warm - palettes[2]).mean(),
                           np.abs(warm - palettes[0]).mean())

    def test_audio_level_jitter_does_not_blink_between_cool_and_warm_colors(self):
        # Alternate levels across the adaptive palette's steep transition.
        # The source stays still so changes measure the audio mapping alone.
        flat = np.full((48, 48, 3), 140, dtype=np.uint8)
        self.controls.update(self.controls.settings_snapshot()[1] | {"slowdown": 95})
        frames = []
        for tick in range(90):
            phase = (np.arange(1024) + tick * 1600) / 48_000
            amplitude = 0.08 if tick % 2 else 0.14
            samples = (amplitude * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
            frames.append(self.texture.render(flat, samples, 1 / 30).astype(np.float32))
        changes = [np.abs(after - before).mean()
                   for before, after in zip(frames[30:], frames[31:])]
        self.assertLess(float(np.mean(changes)), 25)

    def test_steady_bass_does_not_flicker_with_sample_window_phase(self):
        self.controls.update(self.controls.settings_snapshot()[1] | {"slowdown": 95})
        pulses = []
        for tick in range(90):
            phase = (np.arange(1024) + tick * 1600) / 48_000
            samples = (0.045 * np.sin(2 * np.pi * 55 * phase)).astype(np.float32)
            self.texture.render(self.frame, samples, 1 / 30)
            pulses.append(self.texture.pulse)
        self.assertLess(float(np.ptp(pulses[30:])), 0.15)

    def test_short_amplitude_window_does_not_let_old_sound_drive_motion(self):
        phase = np.arange(1024) / 48_000
        samples = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        samples[-256:] = 0
        self.texture.render(self.frame, samples, 1 / 30)
        self.assertEqual(self.texture.pulse, 0)
        np.testing.assert_array_equal(self.texture.band_levels, 0)
        self.assertFalse(self.video.state()["audio_reactive"]["audible"])

    def test_video_motion_selection_uses_the_current_audio_block(self):
        phase = np.arange(1024) / 48_000
        bass = (0.20 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        selected_rates = []

        def select(frame):
            selected_rates.append(self.video.content_fps())
            return frame

        self.texture.render(self.frame, np.zeros(1024), 0, select_frame=select)
        self.texture.render(self.frame, bass, 0, select_frame=select)
        self.assertEqual(selected_rates[0], 10)
        self.assertGreater(selected_rates[1], 28)

    def test_louder_bass_brightens_the_outer_ring(self):
        frame = np.full((48, 48, 3), 100, dtype=np.uint8)
        phase = np.arange(2048) / 48_000
        measurements = []
        for amplitude in (0.02, 0.15):
            controls = PaletteControls(size=48)
            controls.update(default_settings() | {
                "colors": ["#ffffff"], "brightness": 1.0, "slowdown": 0,
            })
            texture = LiveAudioVideoTexture(
                controls, self.video, lambda count: np.zeros(count), size=48,
            )
            bass = (amplitude * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
            for _ in range(45):
                texture.render(frame, bass, 0.05)
            texture.wave_phase = 0.0
            result = texture.render(frame, bass, 0.0)
            radius = texture.radius / 24
            outer = result[(radius > 0.72) & (radius < 1.02)].mean()
            inner = result[radius < 0.30].mean()
            measurements.append((float(outer - inner), float(texture.band_levels[0])))
        self.assertGreater(measurements[1][0], measurements[0][0] + 5)
        self.assertGreater(measurements[1][1], measurements[0][1])

    def test_dominant_frequency_moves_response_inward(self):
        frame = np.full((48, 48, 3), 100, dtype=np.uint8)
        phase = np.arange(2048) / 48_000
        outputs = []
        inner_colors = []
        for frequency in (110, 900, 6000):
            controls = PaletteControls(size=48)
            controls.update(default_settings() | {
                "preset": "custom", "colors": ["#ffffff"],
                "brightness": 1.0, "slowdown": 0,
            })
            texture = LiveAudioVideoTexture(
                controls, self.video, lambda count: np.zeros(count), size=48,
            )
            tone = (0.15 * np.sin(2 * np.pi * frequency * phase)).astype(np.float32)
            for _ in range(10):
                texture.render(frame, tone, 1 / 30)
            # Average a full wave cycle: a travelling trough can temporarily
            # dim any ring, independently of which frequency drives it.
            cycle = []
            for wave_phase in np.linspace(0, 2 * np.pi, 16, endpoint=False):
                texture.wave_phase = wave_phase
                cycle.append(texture.render(frame, tone, 0))
            result = np.mean(cycle, axis=0)
            radius = texture.radius / 24
            inner_colors.append(result[radius < 0.30].mean(axis=0))
            outputs.append((
                result[(radius > 0.72) & (radius < 1.02)].mean(),
                result[(radius > 0.38) & (radius < 0.66)].mean(),
                result[radius < 0.30].mean(),
            ))
        self.assertGreater(outputs[0][0], outputs[2][0] + 2)
        self.assertGreater(outputs[1][1], outputs[2][1] + 0.5)
        # Treble lifts blue in the inner ring; its cool tint can lower the
        # overall RGB average relative to the untinted white bass palette.
        self.assertGreater(inner_colors[2][2], inner_colors[0][2] + 2)
        self.assertGreater(inner_colors[2][2], inner_colors[2][0] + 2)

        bass = (0.15 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        highs = (0.15 * np.sin(2 * np.pi * 6000 * phase)).astype(np.float32)
        for _ in range(25):
            self.texture.render(frame, bass, 0.05)
        self.assertGreater(self.texture.band_levels[0], self.texture.band_levels[2])
        for _ in range(25):
            self.texture.render(frame, highs, 0.05)
        self.assertGreater(self.texture.band_levels[2], self.texture.band_levels[0])

    def test_bass_glow_survives_the_led_brightness_limits(self):
        from led_animator import map_led_intensity
        from stream_arduino import limit_video_flash

        self.controls.update(default_settings() | {
            "colors": ["#ffffff"], "brightness": 0.03, "slowdown": 0,
        })
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        phase = np.arange(2048) / 48_000
        bass = (0.15 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        for _ in range(45):
            self.texture.render(frame, bass, 0.05)
        self.texture.wave_phase = 0.0
        result = self.texture.render(frame, bass, 0.0)
        mapped = limit_video_flash(map_led_intensity(result, 2.2))
        radius = self.texture.radius / 24
        outer = mapped[(radius > 0.72) & (radius < 1.02)].mean()
        inner = mapped[radius < 0.30].mean()
        self.assertGreater(outer, inner)
        self.assertGreater(
            mapped[(radius > 0.72) & (radius < 1.02)].max(),
            mapped[radius < 0.30].max(),
        )

    def test_quiet_music_lowers_content_fps_and_bass_restores_it(self):
        silence = np.zeros(2048, dtype=np.float32)
        phase = np.arange(2048) / 48_000
        bass = (0.18 * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        for _ in range(80):
            self.texture.render(self.frame, silence, 0.05)
        quiet_fps = self.video.state()["content_fps"]
        self.assertLess(quiet_fps, 12)
        for _ in range(50):
            self.texture.render(self.frame, bass, 0.05)
        bass_fps = self.video.state()["content_fps"]
        self.assertGreater(bass_fps, quiet_fps + 12)
        self.assertLessEqual(bass_fps, self.video.fps)
        for _ in range(80):
            self.texture.render(self.frame, silence, 0.05)
        self.assertLess(self.video.state()["content_fps"], bass_fps - 12)


if __name__ == "__main__":
    unittest.main()
