import json
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from app_reload import take_reload_state
from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from board_controls import BoardResetControls
from doom_fire import DoomFire, DoomFireControls
from export_arduino import encode_rgb565
import stream_arduino as video_app
import system_audio_visualizer as audio_app
from stream_arduino import decode_rgb565_frame


def music(amplitude):
    t = np.arange(2048) / 48000
    return (amplitude * (0.5 * np.sin(2 * np.pi * 110 * t)
                        + 0.3 * np.sin(2 * np.pi * 1000 * t)
                        + 0.2 * np.sin(2 * np.pi * 6000 * t))).astype(np.float32)


class DoomFireTests(unittest.TestCase):
    @staticmethod
    def panel_pwm(frame):
        """Include wire quantization and the installed firmware's pixel cap."""
        size = frame.shape[0]
        rgb = decode_rgb565_frame(encode_rgb565(frame), size).astype(np.uint16)
        total = rgb.sum(axis=2, keepdims=True)
        rgb = np.where(total > 64, rgb * 64 // np.maximum(total, 1), rgb)
        return (rgb * 60 // 255).astype(np.uint8)

    def settle(self, fire, samples, *, brightness=1, slowdown=20, frames=120):
        for _ in range(frames):
            frame = fire.render(samples, 1 / 12, brightness=brightness, slowdown=slowdown)
        return frame

    def test_energy_increases_height_and_intensity_at_all_panel_sizes(self):
        for size in (16, 32, 48):
            with self.subTest(size=size):
                quiet, loud = DoomFire(size, seed=7), DoomFire(size, seed=7)
                dim = self.settle(quiet, music(.01))
                hot = self.settle(loud, music(.6))
                self.assertEqual(hot.shape, (size, size, 3))
                self.assertEqual(hot.dtype, np.uint8)
                self.assertGreater(loud.energy, quiet.energy + .5)
                self.assertGreaterEqual(loud.height(), quiet.height() + .5)
                # Quiet colors have a visibility floor now; energetic fire
                # must still be substantially brighter across the panel.
                self.assertGreater(hot.mean(), dim.mean() * 2)
                self.assertTrue(np.any(hot[:size // 3]))
                self.assertTrue(np.any(hot[-1]))
                encoded = np.frombuffer(encode_rgb565(hot), dtype='<u2')
                self.assertGreater(np.count_nonzero(encoded), size * size // 2)

    def test_same_fire_rises_and_falls_with_changes_in_music(self):
        fire = DoomFire(seed=3)
        self.settle(fire, music(.01))
        calm = fire.height()
        self.settle(fire, music(.6))
        peak = fire.height()
        self.settle(fire, music(.01))
        self.assertGreater(peak, calm * 1.5)
        self.assertLess(fire.height(), peak * .7)

    def test_wind_moves_left_by_the_same_random_value_used_for_cooling(self):
        for decay in (0, 1, 2):
            with self.subTest(decay=decay):
                fire = DoomFire(size=48)
                fire.heat[24, 24] = 36
                fire.rng = mock.Mock()
                fire.rng.integers.return_value = np.full((48, 47), decay)
                fire._step()
                # The hot pixel below rises one row and blows left. This
                # checks the reference's wind/cooling coupling, not a palette.
                self.assertAlmostEqual(float(fire.heat[23, 24 - decay]),
                                       36 - decay * 40 / 48, places=5)
                self.assertEqual(np.flatnonzero(fire.heat[23]).tolist(), [24 - decay])

    def test_peak_fire_keeps_a_large_red_region_above_the_hot_base(self):
        fire = DoomFire(size=48, seed=19)
        fire.audible, fire.energy = True, 1.0
        proportions = []
        for step in range(240):
            fire._step()
            if step >= 120:
                lit = fire.heat >= 1
                red = lit & (fire.heat < 18)
                proportions.append(np.count_nonzero(red) / np.count_nonzero(lit))
        # The 40-row reference devotes almost half its lit area to red and
        # orange tips. Energy must never flatten these into a yellow sheet.
        self.assertGreater(float(np.mean(proportions)), .4)
        self.assertGreater(float(fire.heat[-1].min()), 35)

    def test_streamed_fire_keeps_crisp_pixels_without_temporal_smearing(self):
        controls = PaletteControls(size=48)
        controls.update(default_settings() | {'brightness': 1, 'slowdown': 0})
        visualizer = audio_app.AudioVisualizer('doom-fire', controls=controls, size=48)
        visualizer._output_brightness = mock.Mock()
        clock = iter(np.arange(100) / 12)
        with mock.patch.object(audio_app.time, 'monotonic', side_effect=lambda: next(clock)):
            for _ in range(100):
                frame = visualizer.render(music(.6))
        expected = visualizer._fire.palette[
            np.rint(visualizer._fire.heat).clip(0, 36).astype(np.intp)]
        # The deliberate cool ember bed colors the bottom rows separately;
        # the flame above it retains the exact heat-indexed pixels.
        np.testing.assert_array_equal(frame[:-visualizer._fire._base_rows],
                                      expected[:-visualizer._fire._base_rows])

    def test_sudden_cold_to_hot_attack_pumps_fire_with_a_fast_logarithmic_sweep(self):
        fire = DoomFire(seed=5)
        self.settle(fire, music(.01), slowdown=0)
        calm_height = fire.height()
        color_steps = [fire.color_energy]
        for tick in range(4):
            fire.render(music(.6), 1 / 12, slowdown=0)
            color_steps.append(fire.color_energy)
            if tick == 0:
                self.assertGreater(fire.energy, .95)
                self.assertGreater(fire.height(), calm_height + .12)
                self.assertGreater(fire.color_energy, .45)
                self.assertLess(fire.color_energy, .7)
        self.assertTrue(np.all(np.diff(color_steps) >= 0))
        self.assertTrue(np.all(np.diff(np.diff(color_steps)) < 0))
        self.assertGreater(fire.color_energy, .95)
        self.assertGreater(fire.height(), calm_height + .25)
        for _ in range(6):
            fire.render(music(.01), 1 / 12, slowdown=0)
        self.assertGreater(fire.color_energy, fire.energy + .2)

    def test_burst_is_immediate_even_with_slow_motion_and_only_newest_audio_hot(self):
        fire = DoomFire(seed=19)
        self.settle(fire, music(.01), slowdown=95)
        samples = music(.01)
        samples[-512:] = music(.6)[-512:]
        fire.render(samples, 1 / 12, slowdown=95)
        self.assertGreater(fire.energy, .9)
        self.assertGreater(fire.color_energy, .45)
        self.assertGreater(fire.height(), .15)
        for _ in range(4):
            fire.render(music(.6), 1 / 12, slowdown=95)
        self.assertGreater(fire.color_energy, .9)

    def test_gradual_build_keeps_smooth_colors_without_repeated_explosions(self):
        fire = DoomFire(seed=5)
        self.settle(fire, music(.01), slowdown=0)
        colors = [fire.color_energy]
        for amplitude in np.linspace(.01, .04, 36):
            fire.render(music(amplitude), 1 / 12, slowdown=0)
            colors.append(fire.color_energy)
        self.assertGreater(fire.color_energy, colors[0] + .2)
        self.assertLess(float(np.max(np.diff(colors))), .035)
        self.assertEqual(fire._burst_strength, 0)

    def test_isolated_bass_attack_also_triggers_the_air_pulse(self):
        fire = DoomFire(seed=19)
        self.settle(fire, music(.01), slowdown=0)
        t = np.arange(2048) / 48000
        fire.render(np.sin(2 * np.pi * 110 * t).astype(np.float32), 1 / 12, slowdown=0)
        self.assertGreater(fire.energy, .55)
        self.assertGreater(fire._burst_strength, .5)
        self.assertGreater(fire.color_energy, .3)

    def test_batched_scatter_matches_original_order_and_wrap_collisions(self):
        for size in (16, 32, 48):
            fire = DoomFire(size, seed=3)
            fire.audible, fire.energy = True, .9
            fire.heat[:] = np.random.default_rng(1).uniform(0, 36, fire.heat.shape)
            reference = fire.heat.copy().ravel()
            rng = np.random.default_rng(3)
            for _ in range(20):
                reference[-size:] = 10 + 26 * .9
                decays = rng.integers(0, 3, size=size * (size - 1))
                for index, decay in zip((row * size + column for column in range(size)
                                         for row in range(size - 1)), decays):
                    if index - decay >= 0:
                        reference[index - decay] = max(0, float(reference[index + size])
                                                        - decay * 40 / size)
                fire._step()
                np.testing.assert_array_equal(fire.heat.ravel(), reference)

    def test_energetic_base_keeps_cool_regions_and_adds_yellow_on_the_panel(self):
        fire = DoomFire(seed=19)
        frame = self.settle(fire, music(.6), brightness=.514, slowdown=0)
        bottom = self.panel_pwm(frame)[-1].astype(float)
        cool = bottom[:, 2] > bottom[:, 0]
        yellow = (bottom[:, 1] >= bottom[:, 0] * .6) & (bottom[:, 0] > bottom[:, 2] * 2)
        self.assertGreaterEqual(np.count_nonzero(cool), fire.size // 4)
        self.assertGreaterEqual(np.count_nonzero(yellow), fire.size // 8)

    def test_heat_contrast_survives_wire_encoding_and_firmware_brightness_cap(self):
        fire = DoomFire(seed=2)
        self.settle(fire, music(.6), brightness=.514, slowdown=0)
        # Evaluate the actual panel output, where the former bright-red
        # values were all flattened to essentially the same PWM intensity.
        ramp = fire.palette[np.linspace(0, 36, 48).astype(int)]
        pwm = self.panel_pwm(np.tile(ramp[None, :, :], (48, 1, 1)))
        mid = int(pwm[0, 24].sum())
        core = int(pwm[0, -1].sum())
        self.assertGreater(mid, 0)
        self.assertLess(mid, core * .65)
        self.assertGreater(core, 10)
        self.assertLessEqual(int(pwm.sum(axis=2).max()), 15)

    def test_cold_flame_survives_rgb565_and_preserves_visible_purple(self):
        fire = DoomFire(seed=19)
        frame = self.settle(fire, music(.01), brightness=.514, slowdown=0)
        pwm = self.panel_pwm(frame)
        core = fire.heat >= 6
        self.assertGreater(np.count_nonzero(core), 100)
        self.assertGreaterEqual(float(np.median(pwm[..., 2][core])), 3)
        purple = (pwm[..., 0] >= 1) & (pwm[..., 2] >= 3)
        self.assertGreater(np.count_nonzero(purple & core), np.count_nonzero(core) * .75)
        lit = frame.max(axis=2) > 0
        self.assertFalse(np.any(lit & (pwm.max(axis=2) == 0)))
        self.assertLessEqual(int(pwm.sum(axis=2).max()), 15)

    def test_silence_stays_black_and_extinguishes_fire_even_at_maximum_slowdown(self):
        fire = DoomFire(seed=3)
        silence = np.zeros(2048, dtype=np.float32)
        self.assertFalse(np.any(self.settle(fire, silence)))
        self.settle(fire, music(.6))
        extinguished = self.settle(fire, silence, slowdown=95, frames=48)
        self.assertFalse(fire.audible)
        self.assertFalse(np.any(extinguished))
        self.assertEqual(fire.height(), 0)

    def test_brightness_limit_and_zero_apply_to_real_rgb565_output(self):
        fire = DoomFire(seed=1)
        frame = self.settle(fire, music(.6), brightness=16 / 255)
        self.assertLessEqual(int(frame.max()), 16)
        self.assertTrue(np.any(np.frombuffer(encode_rgb565(frame), dtype='<u2')))
        frame = fire.render(music(.6), 1 / 12, brightness=0)
        self.assertFalse(np.any(frame))
        self.assertFalse(any(encode_rgb565(frame)))

    def test_simulation_is_independent_of_output_cadence(self):
        heights = []
        for fps in (12, 30, 60):
            fire = DoomFire(seed=2)
            for _ in range(fps * 8):
                fire.render(music(.6), 1 / fps, brightness=1)
            heights.append(fire.height())
        self.assertLess(max(heights) - min(heights), .1)

    def test_visualizer_switches_in_place_and_publishes_the_encoded_frame(self):
        controls = PaletteControls(size=48)
        switches = DoomFireControls()
        visualizer = audio_app.AudioVisualizer('spectrum', controls=controls, size=48,
                                               fire_controls=switches)
        clock = iter(np.arange(240) / 12)
        with mock.patch.object(audio_app.time, 'monotonic', side_effect=lambda: next(clock)):
            switches.set_active(True)
            for _ in range(120):
                frame = visualizer.render(music(.6))
            self.assertGreater(switches.state()['height'], .5)
            np.testing.assert_array_equal(controls.state()['frame'], frame)
            self.assertEqual(len(encode_rgb565(frame)), 48 * 48 * 2)
            switches.set_active(False)
            frame = visualizer.render(music(.6))
            self.assertEqual(visualizer.style, 'spectrum')
            self.assertFalse(visualizer._fire_active)
            np.testing.assert_array_equal(controls.state()['frame'], frame)
        self.assertEqual(audio_app.build_parser().parse_args(['--style', 'doom-fire']).style, 'doom-fire')


class DoomFireServerTests(unittest.TestCase):
    def start_server(self, **kwargs):
        server = PaletteServer(PaletteControls(), port=0, video_library={}, **kwargs)
        server.start()
        self.addCleanup(server.close)
        return server

    def post(self, server, action='start', payload=b'{}', **headers):
        return urlopen(Request(server.url + '/api/fire/' + action, data=payload, method='POST',
                       headers={'Content-Type': 'application/json', **headers}), timeout=2)

    def test_start_stop_and_validation(self):
        switches = DoomFireControls()
        server = self.start_server(fire_controls=switches)
        for payload, headers, code in ((b'null', {}, 400), (b'{"active":true}', {}, 400),
                                      (b'{}', {'Origin': 'https://example.com'}, 403)):
            with self.assertRaises(HTTPError) as error:
                self.post(server, payload=payload, **headers)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
        self.assertFalse(switches.state()['active'])
        with self.post(server) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)['fire']['active'])
        with self.post(server, 'stop') as response:
            self.assertFalse(json.load(response)['fire']['active'])

    def test_video_handoff_is_single_and_preview_reports_unavailable(self):
        started = mock.Mock()
        server = self.start_server(on_fire_start=started)
        with self.post(server) as response:
            self.assertEqual(response.status, 202)
            self.assertTrue(json.load(response)['starting'])
        # The handler invokes the callback just after writing its response.
        with self.assertRaises(HTTPError) as error:
            self.post(server)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        started.assert_called_once_with()
        preview = self.start_server()
        with self.assertRaises(HTTPError) as error:
            self.post(preview)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_board_reset_blocks_effect_switch(self):
        boards = BoardResetControls()
        switches = DoomFireControls()
        server = self.start_server(fire_controls=switches, board_controls=boards)
        boards.request()
        with self.assertRaises(HTTPError) as error:
            self.post(server)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.assertFalse(switches.state()['active'])


class DoomFireLifecycleTests(unittest.TestCase):
    def test_audio_reload_keeps_fire_selected_after_an_in_place_switch(self):
        server, capture, connection = mock.Mock(port=49123), mock.Mock(), mock.Mock()
        refresh = switches = None

        def make_server(*args, fire_controls, on_refresh, **kwargs):
            nonlocal refresh, switches
            refresh, switches = on_refresh, fire_controls
            return server

        def stream(connection, source, **kwargs):
            switches.set_active(True)
            refresh()
            next(source.iter_frames())

        with (
            mock.patch.object(audio_app, 'AudioCapture', return_value=capture),
            mock.patch.object(audio_app, 'PaletteServer', side_effect=make_server),
            mock.patch.object(audio_app, 'resolve_port', return_value='test-port'),
            mock.patch.object(audio_app, 'open_serial', return_value=connection),
            mock.patch.object(audio_app, 'handshake'),
            mock.patch.object(audio_app, 'stream_frames', side_effect=stream),
            mock.patch.object(audio_app.time, 'sleep'),
            mock.patch.object(audio_app.os, 'execv') as execute,
        ):
            self.assertEqual(audio_app.main(['--no-browser', '--style', 'spectrum']), 0)
        args = audio_app.build_parser().parse_args(execute.call_args.args[1][2:])
        self.assertEqual(args.style, 'doom-fire')
        self.assertEqual(args.controls_port, 49123)
        capture.close.assert_called_once()
        connection.close.assert_called_once()

    def test_video_releases_resources_and_hands_off_with_palette_and_same_browser_port(self):
        events = []
        palette = PaletteControls(size=48)
        palette.update(default_settings() | {'brightness': .18, 'slowdown': 35})
        connection, server = mock.Mock(), mock.Mock(port=49123)
        connection.close.side_effect = lambda: events.append('serial closed')
        server.close.side_effect = lambda: events.append('http closed')
        start = None

        def make_server(*args, on_fire_start, **kwargs):
            nonlocal start
            start = on_fire_start
            return server

        def stream(*args, **kwargs):
            start()
            self.assertTrue(kwargs['should_stop']())
            return 1, 0

        with (
            mock.patch.dict(os.environ),
            mock.patch('audio_palette_controls.PaletteControls', return_value=palette),
            mock.patch('audio_palette_controls.PaletteServer', side_effect=make_server),
            mock.patch.object(video_app, 'probe_video_duration', return_value=60),
            mock.patch.object(video_app, 'probe_video', return_value=mock.Mock(fps=30)),
            mock.patch.object(video_app, 'open_video', return_value=video_app.FrameSource(30, lambda: iter(()))),
            mock.patch.object(video_app, 'resolve_port', return_value='test-port'),
            mock.patch.object(video_app, 'open_serial', return_value=connection),
            mock.patch.object(video_app, 'handshake'),
            mock.patch.object(video_app, 'stream_frames', side_effect=stream),
            mock.patch.object(video_app.time, 'sleep'),
            mock.patch.object(video_app.os, 'execv', side_effect=lambda *a: events.append('exec')) as execute,
        ):
            self.assertEqual(video_app.main(['clip.mp4', '--no-browser', '--no-audio-reactive']), 0)
            saved = take_reload_state()
        self.assertEqual(events, ['http closed', 'serial closed', 'exec'])
        self.assertEqual(saved['palette'], palette.settings_snapshot()[1])
        argv = execute.call_args.args[1]
        self.assertEqual(argv[1], str(Path(audio_app.__file__).resolve()))
        args = audio_app.build_parser().parse_args(argv[2:])
        self.assertEqual(args.style, 'doom-fire')
        self.assertEqual(args.controls_port, 49123)
        self.assertEqual(args.port, 'test-port')
        self.assertEqual(args.fps, 30)
        self.assertTrue(args.no_browser)


if __name__ == '__main__':
    unittest.main()
