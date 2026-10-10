import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

import stream_arduino as stream
import system_audio_visualizer as audio_app
from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from led_animator import VideoInfo
from video_controls import VideoPlaybackControls
from video_motion import LiveAudioVideoMotion, MotionVideoTimeline, bend_video_texture, speed_for_energy, spectral_fullness, tint_video
from video_audio_texture import make_video_energy_palette
from app_reload import take_reload_state


class VideoMotionTests(unittest.TestCase):
    def test_speed_maps_full_energy_range_linearly_from_point_eight_to_one_point_five(self):
        self.assertEqual(speed_for_energy(0), 0.8)
        self.assertAlmostEqual(speed_for_energy(.1), .87)
        self.assertAlmostEqual(speed_for_energy(.5), 1.15)
        self.assertEqual(speed_for_energy(-1), 0.8)
        self.assertEqual(speed_for_energy(2), 1.5)
        self.assertEqual(speed_for_energy(1), 1.5)
        speeds = [speed_for_energy(energy) for energy in np.linspace(0, 1, 101)]
        self.assertTrue(all(a <= b for a, b in zip(speeds, speeds[1:])))

    def test_speed_controls_accept_new_range_and_reject_invalid_rates(self):
        controls = VideoPlaybackControls("clip.mp4", 60, 30, audio_motion=True)
        for speed in (.8, 1, 1.15, 1.5):
            controls.set_playback_speed(speed)
            self.assertEqual(controls.playback_speed(), speed)
        for speed in (.79, 1.51, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                controls.set_playback_speed(speed)

    def test_relative_music_energy_drives_tint_saturation_brightness_and_speed(self):
        controls = PaletteControls(size=16, slowdown=0)
        controls.update(default_settings() | {"preset": "adaptive", "slowdown": 0, "brightness": 1})
        video = VideoPlaybackControls("liquid.mp4", 60, 30, audio_motion=True)
        motion = LiveAudioVideoMotion(controls, video, lambda count: np.zeros(count), size=16)
        frame = np.full((16, 16, 3), [40, 100, 200], dtype=np.uint8)
        t = np.arange(4096) / 48_000
        chorus = sum(.15 * np.sin(2 * np.pi * frequency * t)
                     for frequency in (110, 880, 6000)).astype(np.float32)
        calm = motion.render(frame, np.zeros(4096), 1 / 30)
        self.assertEqual(video.playback_speed(), .8)
        self.assertGreater(float(calm[..., 2].mean()), float(calm[..., 0].mean()))
        calm_saturation = video.state()["audio_reactive"]["saturation"]

        for _ in range(240):
            result = motion.render(frame, chorus, 1 / 30)
        self.assertGreater(video.playback_speed(), 1.49)
        self.assertGreater(float(result[..., 0].mean()), float(result[..., 2].mean()) * 2)
        self.assertGreater(float(stream.map_led_intensity(result, 2.2).mean()),
                           float(stream.map_led_intensity(calm, 2.2).mean()) * 2)
        self.assertGreater(video.state()["audio_reactive"]["saturation"], calm_saturation)
        self.assertFalse(video.texture_mapping_enabled())
        with self.assertRaises(ValueError):
            video.set_layers({"audio_palette": True})
        for _ in range(300):
            motion.render(frame, chorus * .5, 1 / 30)
        self.assertLess(video.playback_speed(), .85)
        for _ in range(240):
            motion.render(frame, chorus, 1 / 30)
        self.assertGreater(video.playback_speed(), 1.49)
        motion.render(frame, np.zeros(4096), 1 / 30)
        self.assertEqual(video.playback_speed(), .8)
        self.assertFalse(video.state()["audio_reactive"]["audible"])

    def test_maximum_drive_requires_all_three_bands_even_at_equal_rms(self):
        t = np.arange(4096) / 48_000
        speeds, intensities = [], []
        for frequencies in ((110,), (880,), (6000,), (110, 880), (110, 6000), (880, 6000), (110, 880, 6000)):
            controls = PaletteControls(size=16, slowdown=0)
            controls.update(default_settings() | {"brightness": 1, "slowdown": 0})
            video = VideoPlaybackControls("clip.mp4", 60, 30, audio_motion=True)
            motion = LiveAudioVideoMotion(controls, video, lambda count: np.zeros(count), size=16)
            samples = sum(.3 / np.sqrt(len(frequencies)) * np.sin(2 * np.pi * frequency * t)
                          for frequency in frequencies).astype(np.float32)
            for _ in range(240):
                frame = motion.render(np.full((16, 16, 3), 220, dtype=np.uint8), samples, 1 / 30)
            speeds.append(video.playback_speed())
            intensities.append(float(stream.map_led_intensity(frame, 2.2).mean()))
        self.assertTrue(all(speed < 1.2 for speed in speeds[:-1]), speeds)
        self.assertGreater(speeds[-1], 1.49)
        self.assertGreater(intensities[-1], max(intensities[:-1]) * 1.5)
        self.assertEqual(spectral_fullness(np.zeros(3)), 0)

    def test_tint_keeps_black_and_video_detail_and_honors_manual_controls(self):
        ramp = np.arange(16, dtype=np.uint8) * 17
        frame = np.broadcast_to(ramp[None, :, None], (16, 16, 3)).copy()
        for energy in (0, .5, 1):
            palette = make_video_energy_palette(energy, size=16)
            full = tint_video(frame, palette, saturation=1, brightness=1)
            dim = tint_video(frame, palette, saturation=1, brightness=.25)
            gray = tint_video(frame, palette, saturation=0, brightness=1)
            np.testing.assert_array_equal(full[:, 0], 0)
            self.assertGreater(np.unique(full.reshape(-1, 3), axis=0).shape[0], 12)
            self.assertLess(float(dim.mean()), float(full.mean()))
            np.testing.assert_array_equal(gray[..., 0], gray[..., 1])
            np.testing.assert_array_equal(gray[..., 1], gray[..., 2])
            np.testing.assert_array_equal(tint_video(frame, palette, saturation=1, brightness=0), 0)

    def test_peak_wave_bends_video_smoothly_and_keeps_the_border_and_black_background(self):
        for size in (16, 32, 48):
            with self.subTest(size=size):
                y, x = np.indices((size, size))
                frame = np.repeat(((x * 9 + y * 5) % 256)[..., None], 3, axis=2).astype(np.uint8)
                for energy in (0, .5, .82):
                    calm, shift = bend_video_texture(frame, energy, .7)
                    np.testing.assert_array_equal(calm, frame)
                    self.assertIsNone(shift)
                start, _ = bend_video_texture(frame, .821, .7)
                bent, shift = bend_video_texture(frame, 1, .7)
                later, later_shift = bend_video_texture(frame, 1, .8)
                self.assertLess(float(np.abs(start - frame).max()), .1)
                self.assertGreater(float(np.abs(bent - frame).max()), 1)
                self.assertFalse(np.array_equal(bent, later))
                self.assertFalse(np.array_equal(shift, later_shift))
                np.testing.assert_allclose(bent[[0, -1]], frame[[0, -1]], atol=1e-4)
                np.testing.assert_allclose(bent[:, [0, -1]], frame[:, [0, -1]], atol=1e-4)
                black, shift = bend_video_texture(np.zeros_like(frame), 1, .7)
                np.testing.assert_array_equal(tint_video(black, make_video_energy_palette(1, size=size),
                    saturation=1, brightness=1, palette_shift=shift), 0)

    def test_peak_color_wave_follows_video_detail_and_respects_brightness_and_saturation(self):
        frame = np.full((48, 48, 3), 180, dtype=np.uint8)
        frame[16:32, 16:32] = 60
        palette = make_video_energy_palette(1)
        bent, shift = bend_video_texture(frame, 1, .7)
        _, flat_shift = bend_video_texture(np.full_like(frame, 180), 1, .7)
        self.assertGreater(float(np.abs(shift - flat_shift).max()), .1)
        result = tint_video(bent, palette, saturation=1, brightness=1, palette_shift=shift)
        dim = tint_video(bent, palette, saturation=1, brightness=.25, palette_shift=shift)
        gray = tint_video(bent, palette, saturation=0, brightness=1, palette_shift=shift)
        self.assertGreater(np.unique(result[:16].reshape(-1, 3), axis=0).shape[0], 50)
        self.assertLess(float(dim.mean()), float(result.mean()))
        self.assertLess(float(result[20:28, 20:28].mean()), float(result[:16].mean()))
        np.testing.assert_array_equal(gray[..., 0], gray[..., 1])
        np.testing.assert_array_equal(gray[..., 1], gray[..., 2])
        np.testing.assert_array_equal(frame[16:32, 16:32], 60)

    def test_live_peak_wave_animates_selected_video_and_releases_in_silence(self):
        controls = PaletteControls(size=48)
        controls.update(default_settings() | {"brightness": 1, "slowdown": 0})
        video = VideoPlaybackControls("clip.mp4", 60, 30, audio_motion=True)
        motion = LiveAudioVideoMotion(controls, video, lambda count: np.zeros(count))
        t = np.arange(4096) / 48_000
        chorus = sum(.15 * np.sin(2 * np.pi * frequency * t)
                     for frequency in (110, 880, 6000)).astype(np.float32)
        source = np.full((48, 48, 3), 180, dtype=np.uint8)
        for _ in range(240):
            first = motion.render(source, chorus, 1 / 30)
        self.assertGreater(video.state()["audio_reactive"]["energy"], .99)
        second = motion.render(np.zeros_like(source), chorus, 1 / 30, select_frame=lambda frame: source)
        self.assertGreater(float(np.abs(second.astype(float) - first).max()), 0)
        calm = motion.render(source, np.zeros(4096), 1 / 30)
        np.testing.assert_array_equal(calm, tint_video(source, make_video_energy_palette(0),
            saturation=.55, brightness=1))

    def test_tint_uses_the_newly_selected_frame_and_hidden_video_stays_black(self):
        controls = PaletteControls(size=16)
        controls.update(default_settings() | {"brightness": 1})
        video = VideoPlaybackControls("clip.mp4", 60, 30, audio_motion=True)
        motion = LiveAudioVideoMotion(controls, video, lambda count: np.zeros(count), size=16)
        black = np.zeros((16, 16, 3), dtype=np.uint8)
        selected = np.full_like(black, 200)
        result = motion.render(black, np.zeros(4096), 1 / 30, select_frame=lambda frame: selected)
        self.assertGreater(int(result.max()), 0)
        np.testing.assert_array_equal(black, 0)
        video.set_layers({"video": False})
        np.testing.assert_array_equal(motion.render(selected, np.zeros(4096), 1 / 30), 0)

    def test_calm_tint_survives_led_gamma_and_rgb565_and_warm_output_is_brighter(self):
        source = np.full((48, 48, 3), 128, dtype=np.uint8)
        output = []
        for gamma in (1, 2.2, 3):
            for energy, saturation in ((0, .55), (1, 1)):
                raw = tint_video(source, make_video_energy_palette(energy),
                                 saturation=saturation, brightness=1, gamma=gamma)
                limiter = stream.VideoFlashLimiter(30)
                for _ in range(60):
                    limited = limiter.apply(stream.map_led_intensity(raw, gamma))
                encoded = stream.encode_limited_video_rgb565(limited)
                output.append(stream.decode_rgb565_frame(encoded, 48))
            calm, warm = output[-2:]
            self.assertGreater(int(calm.max()), 0)
            self.assertGreater(float(calm[..., 2].mean()), float(calm[..., 0].mean()))
            self.assertGreater(float(warm.mean()), float(calm.mean()))
            self.assertGreater(float(warm[..., 0].mean()), float(warm[..., 2].mean()))

    def test_timeline_consumes_source_frames_at_speed_and_handles_pause_seek_and_close(self):
        closed = []

        def decode(seconds):
            try:
                for index in range(120):
                    yield np.full((16, 16, 3), index, dtype=np.uint8)
            finally:
                closed.append(seconds)

        for output_fps in (15, 30):
            with self.subTest(output_fps=output_fps):
                controls = VideoPlaybackControls("clip.mp4", 60, output_fps, audio_motion=True)
                timeline = MotionVideoTimeline(30, controls, decode)
                frames = timeline.frames_at(5)
                values = []
                for speed in (.8, .8, 1, 1.5):
                    controls.set_playback_speed(speed)
                    values.append(int(timeline.select(np.frombuffer(next(frames), dtype=np.uint8))[0, 0, 0]))
                scale = 30 // output_fps
                self.assertEqual(values, [0, int(.8 * scale), int(1.8 * scale), int(3.3 * scale)])
                self.assertAlmostEqual(controls.state()["position"], 5 + int(3.3 * scale) / 30)
                controls.set_paused(True)
                timeline.select(np.frombuffer(next(frames), dtype=np.uint8))
                self.assertAlmostEqual(controls.state()["position"], 5 + int(3.3 * scale) / 30)
                controls.set_paused(False)
                frames.close()
                self.assertEqual(closed[-1], 5)
                seeked = timeline.frames_at(20)
                timeline.select(np.frombuffer(next(seeked), dtype=np.uint8))
                self.assertEqual(controls.state()["position"], 20)
                seeked.close()

    def test_timeline_eof_stops_and_repeating_resets_position(self):
        controls = VideoPlaybackControls("clip.mp4", 1, 30, audio_motion=True)
        controls.set_playback_speed(1.5)
        timeline = MotionVideoTimeline(30, controls, lambda seconds: iter([
            np.full((16, 16, 3), index, dtype=np.uint8) for index in range(5)
        ]))
        for _ in range(2):
            values = []
            for raw in timeline.frames_at():
                values.append(int(timeline.select(np.frombuffer(raw, dtype=np.uint8))[0, 0, 0]))
            self.assertEqual(values, [0, 1, 3, 4, 4])

    def test_stream_keeps_panel_cadence_and_reports_accelerated_position(self):
        controls = VideoPlaybackControls("clip.mp4", 60, 30, audio_motion=True)
        palette = PaletteControls(size=16)
        texture = LiveAudioVideoMotion(palette, controls, lambda count: np.zeros(count), size=16)
        now = [0.0]
        positions, send_times = [], []

        def fixed_energy(frame, *, select_frame):
            controls.set_playback_speed(1.5)
            return select_frame(frame)

        def sent(*args):
            positions.append(controls.state()["position"])
            send_times.append(now[0])

        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(stream, "probe_video", return_value=VideoInfo(16, 16, 30)),
            mock.patch.object(stream, "iter_square_video_frames", side_effect=lambda *a, **k: iter([
                np.full((16, 16, 3), index, dtype=np.uint8) for index in range(100)
            ])),
            mock.patch.object(texture, "apply", side_effect=fixed_energy),
            mock.patch.object(stream.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(stream.time, "sleep", side_effect=lambda dt: now.__setitem__(0, now[0] + dt)),
            mock.patch.object(stream, "exchange_packet", side_effect=sent),
        ):
            source = stream.open_video(Path("clip.mp4"), size=16, texture=texture)
            self.assertTrue(source.maintains_position)
            stream.stream_frames(mock.Mock(), source, loop=True, timeout=1, retries=0,
                drop_late=False, rebase_late=True, controls=controls,
                should_stop=lambda: len(positions) >= 10)
        np.testing.assert_allclose(positions, np.floor(np.arange(10) * 1.5) / 30)
        np.testing.assert_allclose(send_times, np.arange(10) / 30)
        self.assertAlmostEqual(controls.state()["position"], 13 / 30)

    def test_motion_start_accepts_selected_download_and_validates_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            liquid = Path(directory) / "Abstract Liquid Background [pUWCnazcsbU].mp4"
            liquid.touch()
            selected = Path(directory) / "Objects Impact.mp4"
            selected.touch()
            selected_key = ".build/youtube/Objects Impact.mp4"
            started = threading.Event()
            callback = mock.Mock(side_effect=lambda path: started.set())
            server = PaletteServer(PaletteControls(), port=0, on_motion_start=callback,
                video_library={liquid.name: liquid, selected_key: selected})
            server.start()
            try:
                with urlopen(server.url + "/api/state") as response:
                    state = json.load(response)
                self.assertIn(selected_key, state["videos"])
                self.assertTrue(state["motion_start_available"])

                def post(payload, origin=None):
                    headers = {"Content-Type": "application/json"}
                    if origin:
                        headers["Origin"] = origin
                    return urlopen(Request(server.url + "/api/video/motion/start",
                        data=json.dumps(payload).encode(), method="POST", headers=headers), timeout=2)

                for payload, origin, code in (({"video": selected_key}, "https://example.com", 403),
                                              ({}, None, 400),
                                              ({"video": "../clip.mp4"}, None, 400),
                                              ({"video": "missing.mp4"}, None, 400),
                                              ({"video": [selected_key]}, None, 400),
                                              ({"video": selected_key, "extra": True}, None, 400),
                                              ([], None, 400)):
                    with self.subTest(payload=payload, origin=origin), self.assertRaises(HTTPError) as error:
                        post(payload, origin)
                    self.assertEqual(error.exception.code, code)
                    error.exception.close()
                with post({"video": selected_key}) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(started.wait(1))
                callback.assert_called_once_with(selected)
                with self.assertRaises(HTTPError) as error:
                    post({"video": selected_key})
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
            finally:
                server.close()

    def test_removed_selection_is_reported_without_starting_another_video(self):
        callback = mock.Mock()
        server = PaletteServer(PaletteControls(), port=0, on_motion_start=callback,
                               video_library={"other.mp4": Path("other.mp4")})
        server.start()
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(server.url + "/api/video/motion/start", data=b'{"video":"other.mp4"}', method="POST",
                                headers={"Content-Type": "application/json"}), timeout=2)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            callback.assert_not_called()
        finally:
            server.close()

    def test_switching_video_preserves_active_motion_mode(self):
        for active in (False, True):
            with self.subTest(active=active), mock.patch.dict(os.environ):
                server = mock.Mock(port=49123)
                palette = PaletteControls(size=48)
                callback = None

                def make_server(*args, on_video_start, **kwargs):
                    nonlocal callback
                    callback = on_video_start
                    return server

                def run_stream(*args, **kwargs):
                    callback(Path("/tmp/another-download.mp4"))
                    return 0, 0

                with (
                    mock.patch("audio_palette_controls.PaletteControls", return_value=palette),
                    mock.patch("audio_palette_controls.PaletteServer", side_effect=make_server),
                    mock.patch.object(audio_app, "AudioCapture"),
                    mock.patch.object(stream, "probe_video_duration", return_value=60),
                    mock.patch.object(stream, "probe_video", return_value=VideoInfo(48, 48, 30)),
                    mock.patch.object(stream, "open_video", return_value=stream.FrameSource(30, lambda: iter(()))),
                    mock.patch.object(stream, "resolve_port", return_value="test-port"),
                    mock.patch.object(stream, "open_serial"),
                    mock.patch.object(stream, "handshake"),
                    mock.patch.object(stream, "stream_frames", side_effect=run_stream),
                    mock.patch.object(stream.time, "sleep"),
                    mock.patch.object(stream.os, "execv") as execute,
                ):
                    args = ["clip.mp4", "--no-browser"] + (["--audio-motion"] if active else [])
                    self.assertEqual(stream.main(args), 0)
                options = stream.build_parser().parse_args(execute.call_args.args[1][2:])
                self.assertEqual(options.audio_motion, active)
                self.assertEqual(options.video, Path("/tmp/another-download.mp4"))
                self.assertEqual(take_reload_state()["video"]["paused"], False)

    def test_audio_app_switch_passes_motion_flag_to_video_player(self):
        server = mock.Mock(port=49123)
        callback = None

        def make_server(*args, on_motion_start, **kwargs):
            nonlocal callback
            callback = on_motion_start
            return server

        def run_stream(connection, source, **kwargs):
            callback(Path("/tmp/liquid.mp4"))
            next(source.iter_frames())

        with (
            mock.patch.dict(os.environ),
            mock.patch.object(audio_app, "AudioCapture"),
            mock.patch.object(audio_app, "PaletteServer", side_effect=make_server),
            mock.patch.object(audio_app, "resolve_port", return_value="test-port"),
            mock.patch.object(audio_app, "open_serial"),
            mock.patch.object(audio_app, "handshake"),
            mock.patch.object(audio_app, "stream_frames", side_effect=run_stream),
            mock.patch.object(audio_app.time, "sleep"),
            mock.patch.object(audio_app.os, "execv") as execute,
        ):
            self.assertEqual(audio_app.main(["--no-browser"]), 0)
            self.assertEqual(take_reload_state()["palette"]["slowdown"], 20)
        options = stream.build_parser().parse_args(execute.call_args.args[1][2:])
        self.assertTrue(options.audio_motion)
        self.assertTrue(options.loop)
        self.assertEqual(options.video, Path("/tmp/liquid.mp4"))
        self.assertEqual(options.controls_port, 49123)


if __name__ == "__main__":
    unittest.main()
