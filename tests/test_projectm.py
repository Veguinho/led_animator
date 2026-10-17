"""Control, lifecycle and real OpenGL checks for the projectM LED port."""
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from doom_fire import DoomFireControls
from board_controls import BoardResetControls
import projectm_controls as pm
import system_audio_visualizer as audio
import stream_arduino as video
from app_reload import take_reload_state


class ProjectMTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("Waves.milk", "Particles.milk", "Fractal.milk"):
            (self.root / name).write_text("[preset00]\n")
        self.binary = self.root / "renderer"
        self.binary.touch()
        for name, value in (("PRESETS", self.root), ("BINARY", self.binary),
                            ("FAVORITES_PATH", self.root / "favorites.json")):
            patch = mock.patch.object(pm, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def server(self, **kwargs):
        controls = PaletteControls(size=48)
        server = PaletteServer(controls, 0, video_library={}, **kwargs)
        server.start()
        self.addCleanup(server.close)
        return server, controls

    def post(self, server, action, payload=None, **headers):
        request = Request(server.url + "/api/projectm/" + action,
                          json.dumps({} if payload is None else payload).encode(),
                          {"Content-Type": "application/json", **headers})
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def test_validation_automatic_changes_and_unavailable_setup(self):
        controls = pm.ProjectMControls(48)
        for action, payload in (("preset", {"preset": True}), ("preset", {"preset": 3}),
                                ("preset", {"preset": "../../etc/passwd"}),
                                ("settings", {"auto": True, "duration": 5}),
                                ("settings", {"auto": 1, "duration": 30}), ("start", {"shell": "cmd"}),
                                ("randomize", {"preset": 1}), ("favorite", {"preset": True, "favorite": True}),
                                ("favorite", {"preset": 0, "favorite": 1}), ("favorite-play", {"preset": -1})):
            with self.subTest(action=action, payload=payload), self.assertRaises(ValueError):
                controls.action(action, payload)
        with mock.patch.object(pm.time, "monotonic", return_value=10):
            controls.action("start", {})
        initial = controls.state()["preset"]
        renderer = mock.Mock()
        renderer.render.return_value = np.full((48, 48, 3), 200, np.uint8)
        with (mock.patch.object(pm, "ProjectMRenderer", return_value=renderer),
              mock.patch.object(pm.time, "monotonic", return_value=41)):
            frame = controls.render(np.zeros(2048), .25, 1)
        self.assertNotEqual(controls.state()["preset"], initial)
        np.testing.assert_array_equal(frame, np.full((48, 48, 3), 50, np.uint8))
        controls.action("stop", {})
        self.assertIsNone(controls.render(np.zeros(2048), 1, 1))
        renderer.close.assert_called_once()
        self.binary.unlink()
        with self.assertRaisesRegex(ValueError, "setup_projectm"):
            controls.action("start", {})

    def test_randomize_plays_any_preset_and_holds_it_past_the_timer(self):
        controls = pm.ProjectMControls(48)
        renderer = mock.Mock()
        renderer.render.return_value = np.zeros((48, 48, 3), np.uint8)
        self.addCleanup(controls.close)
        for index in range(len(controls.library())):
            with self.subTest(index=index):
                controls.action("settings", {"auto": True, "duration": 10})
                with (mock.patch.object(pm.random, "randrange", return_value=index) as choose,
                      mock.patch.object(pm.time, "monotonic", return_value=10)):
                    controls.action("randomize", {})
                choose.assert_called_once_with(3)
                state = controls.state()
                self.assertTrue(state["active"])
                self.assertFalse(state["auto"])
                self.assertEqual(state["preset"], index)
                with (mock.patch.object(pm, "ProjectMRenderer", return_value=renderer),
                      mock.patch.object(pm.time, "monotonic", return_value=1000),
                      mock.patch.object(pm.random, "randrange") as timer_choice):
                    controls.render(np.zeros(2048), 1, 1)
                timer_choice.assert_not_called()
                self.assertEqual(controls.state()["preset"], index)
                self.assertEqual(renderer.render.call_args.args[1], index)
        controls.action("stop", {})
        controls._presets = controls._presets[:1]
        controls.action("randomize", {})
        self.assertEqual(controls.state()["preset"], 0)
        self.binary.unlink()
        with self.assertRaisesRegex(ValueError, "setup_projectm"):
            controls.action("randomize", {})

    def test_native_failure_returns_to_audio_and_exposes_error(self):
        controls = PaletteControls(size=48)
        controls.projectm.action("start", {})
        visualizer = audio.AudioVisualizer("wave", controls=controls, size=48)
        with mock.patch.object(pm, "ProjectMRenderer", side_effect=RuntimeError("GPU unavailable")):
            frame = visualizer.render(np.zeros(4000, dtype=np.float32))
        self.assertEqual(frame.shape, (48, 48, 3))
        self.assertFalse(controls.projectm.state()["active"])
        self.assertIn("GPU unavailable", controls.projectm.state()["error"])

    def test_favorites_persist_by_path_when_library_indices_change(self):
        controls = pm.ProjectMControls(48)
        before = controls.state()
        controls.action("favorite", {"preset": 2, "favorite": True})
        controls.action("favorite", {"preset": 2, "favorite": True})
        self.assertEqual(controls.state()["favorites"], [2])
        for key in ("preset", "active", "auto"):
            self.assertEqual(controls.state()[key], before[key])
        self.assertEqual(json.loads(pm.FAVORITES_PATH.read_text()), ["Waves.milk"])
        (self.root / "AAA.milk").write_text("[preset00]\n")
        restored = pm.ProjectMControls(48)
        self.assertEqual(restored.state()["favorites"], [3])
        restored.action("favorite-play", {"preset": 3})
        self.assertEqual(restored.state()["name"], "Waves")
        self.assertTrue(restored.state()["active"])
        self.assertFalse(restored.state()["auto"])
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                restored.action("favorite", {"preset": 3, "favorite": False})
        self.assertEqual(restored.state()["favorites"], [3])
        restored.action("favorite", {"preset": 3, "favorite": False})
        self.assertEqual(pm.ProjectMControls(48).state()["favorites"], [])
        with self.assertRaisesRegex(ValueError, "favorites"):
            restored.action("favorite-play", {"preset": 3})

    def test_projectm_keeps_its_colors_and_obeys_shared_brightness_saturation(self):
        controls = PaletteControls(size=48)
        controls.update(default_settings() | {"brightness": .5, "saturation": 1})
        visualizer = audio.AudioVisualizer("projectm", controls=controls, size=48)
        renderer = mock.Mock()
        renderer.render.return_value = np.broadcast_to(np.array([0, 200, 100], np.uint8), (48, 48, 3))
        with mock.patch.object(pm, "ProjectMRenderer", return_value=renderer):
            frame = visualizer.render(np.zeros(4000))
            np.testing.assert_array_equal(frame[0, 0], [0, 100, 50])
            controls.update(default_settings() | {"brightness": .5, "saturation": 0})
            frame = visualizer.render(np.zeros(4000))
            self.assertEqual(len(set(frame[0, 0])), 1)
        controls.projectm.close()

    def test_http_mode_switches_origin_validation_and_busy_reset(self):
        fire = DoomFireControls(active=True)
        board = BoardResetControls()
        server, controls = self.server(fire_controls=fire, board_controls=board)
        with urlopen(server.url + "/api/projectm/presets") as response:
            self.assertEqual(len(json.load(response)["presets"]), 3)
        _, result = self.post(server, "start")
        self.assertTrue(result["projectm"]["active"])
        self.assertFalse(result["fire"]["active"])
        self.post(server, "favorite", {"preset": 2, "favorite": True})
        self.post(server, "stop")
        fire.set_active(True)
        _, result = self.post(server, "favorite-play", {"preset": 2})
        self.assertEqual(result["projectm"]["favorites"], [2])
        self.assertEqual(result["projectm"]["preset"], 2)
        self.assertTrue(result["projectm"]["active"])
        self.assertFalse(result["projectm"]["auto"])
        self.assertFalse(result["fire"]["active"])
        self.post(server, "stop")
        fire.set_active(True)
        _, result = self.post(server, "randomize")
        self.assertTrue(result["projectm"]["active"])
        self.assertFalse(result["projectm"]["auto"])
        self.assertFalse(result["fire"]["active"])
        self.post(server, "preset", {"preset": 2})
        self.assertEqual(controls.projectm.state()["preset"], 2)
        self.post(server, "settings", {"auto": False, "duration": 60})
        self.assertFalse(controls.projectm.state()["auto"])
        with self.assertRaises(HTTPError) as failure:
            self.post(server, "stop", Origin="https://example.com")
        self.assertEqual(failure.exception.code, 403)
        failure.exception.close()
        with self.assertRaises(HTTPError) as failure:
            self.post(server, "preset", {"preset": -1})
        self.assertEqual(failure.exception.code, 400)
        failure.exception.close()
        request = Request(server.url + "/api/fire/start", b"{}", {"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertFalse(json.load(response)["projectm"]["active"])
        board.request()
        with self.assertRaises(HTTPError) as failure:
            self.post(server, "randomize")
        self.assertEqual(failure.exception.code, 409)
        failure.exception.close()

    def test_audio_reload_preserves_selected_preset_and_auto_settings(self):
        controls = PaletteControls(size=48)
        controls.projectm.action("preset", {"preset": 2})
        controls.projectm.action("settings", {"auto": False, "duration": 120})
        server, capture, connection = mock.Mock(port=49123), mock.Mock(), mock.Mock()
        callback = None

        def make_server(*args, on_refresh, **kwargs):
            nonlocal callback
            callback = on_refresh
            return server

        def stream(*args, **kwargs):
            callback()
            raise audio.RefreshRequested()

        with (mock.patch.dict(os.environ),
              mock.patch.object(audio, "PaletteControls", return_value=controls),
              mock.patch.object(audio, "AudioCapture", return_value=capture),
              mock.patch.object(audio, "PaletteServer", side_effect=make_server),
              mock.patch.object(audio, "resolve_port", return_value="test-port"),
              mock.patch.object(audio, "open_serial", return_value=connection),
              mock.patch.object(audio, "handshake"), mock.patch.object(audio.time, "sleep"),
              mock.patch.object(audio, "stream_frames", side_effect=stream),
              mock.patch.object(audio.os, "execv") as execute):
            self.assertEqual(audio.main(["--style", "projectm", "--no-browser"]), 0)
            state = take_reload_state()["projectm"]
        self.assertEqual((state["preset"], state["auto"], state["duration"]), (2, False, 120))
        self.assertEqual(audio.build_parser().parse_args(execute.call_args.args[1][2:]).style, "projectm")

    def test_video_launch_acknowledges_and_preview_cannot_start(self):
        started = threading.Event()
        server, _ = self.server(on_projectm_start=started.set)
        code, result = self.post(server, "start")
        self.assertEqual(code, 202)
        self.assertTrue(result["starting"])
        self.assertTrue(started.wait(1))
        preview, _ = self.server()
        with self.assertRaises(HTTPError) as failure:
            self.post(preview, "start")
        self.assertEqual(failure.exception.code, 503)
        failure.exception.close()

    @unittest.skipUnless(shutil.which("node") and
                         (pm.ROOT / ".build/ui-check/node_modules/playwright-core").is_dir() and
                         Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file(),
                         "requires local playwright-core and Chrome")
    def test_browser_controls_and_preview(self):
        for index in range(300):
            (self.root / f"Library {index:03d}.milk").write_text("[preset00]\n")
        server, controls = self.server(fire_controls=DoomFireControls())
        controls.publish_frame(np.full((48, 48, 3), 16, np.uint8))
        result = subprocess.run(
            ["node", str(pm.ROOT / "tests/projectm_browser_check.cjs"), server.url],
            env=os.environ | {
                "PROJECTM_PLAYWRIGHT_MODULE": str(pm.ROOT / ".build/ui-check/node_modules/playwright-core"),
                "PROJECTM_CHROME": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            }, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_video_to_projectm_releases_resources_before_restarting(self):
        events = []
        server, connection = mock.Mock(port=49123), mock.Mock()
        server.close.side_effect = lambda: events.append("http closed")
        connection.close.side_effect = lambda: events.append("serial closed")
        callback = None

        def make_server(*args, on_projectm_start, **kwargs):
            nonlocal callback
            callback = on_projectm_start
            return server

        def stream(*args, **kwargs):
            callback()
            self.assertTrue(kwargs["should_stop"]())
            return 1, 0

        with (mock.patch.dict(os.environ),
              mock.patch.object(video, "probe_video_duration", return_value=60),
              mock.patch.object(video, "probe_video", return_value=mock.Mock(fps=30)),
              mock.patch.object(video, "open_video", return_value=video.FrameSource(30, lambda: iter(()))),
              mock.patch("audio_palette_controls.PaletteServer", side_effect=make_server),
              mock.patch.object(video, "resolve_port", return_value="test-port"),
              mock.patch.object(video, "open_serial", return_value=connection),
              mock.patch.object(video, "handshake"), mock.patch.object(video.time, "sleep"),
              mock.patch.object(video, "stream_frames", side_effect=stream),
              mock.patch.object(video.os, "execv", side_effect=lambda *a: events.append("exec")) as execute):
            self.assertEqual(video.main(["test.mp4", "--no-audio-reactive", "--no-browser"]), 0)
            take_reload_state()
        self.assertEqual(events, ["http closed", "serial closed", "exec"])
        argv = execute.call_args.args[1]
        restarted = audio.build_parser().parse_args(argv[2:])
        self.assertEqual((restarted.style, restarted.controls_port, restarted.port), ("projectm", 49123, "test-port"))


@unittest.skipUnless(pm.BINARY.is_file() and sys.platform == "darwin", "run scripts/setup_projectm.py on macOS first")
class NativeProjectMTests(unittest.TestCase):
    def test_real_shader_pcm_orientation_and_area_downsample(self):
        # A UV gradient makes averaging and the OpenGL vertical flip measurable.
        # The blue channel comes from real projectM bass analysis, proving PCM
        # reaches the engine rather than an unrelated animation approximation.
        with tempfile.TemporaryDirectory() as directory:
            preset = Path(directory) / "gradient.milk"
            preset.write_text("MILKDROP_PRESET_VERSION=201\nPSVERSION=2\nPSVERSION_COMP=2\n"
                              "[preset00]\nfDecay=0\nfWaveAlpha=0\n"
                              "comp_1=`shader_body\ncomp_2=`{\n"
                              "comp_3=`ret = float3(uv.x, uv.y, min(bass*0.15, 1));\ncomp_4=`}\n")
            renderer = pm.ProjectMRenderer([preset], 48, 12)
            self.addCleanup(renderer.close)
            silent = renderer.render(np.zeros(4000, np.float32), 0, 1)
            self.assertEqual(silent.shape, (48, 48, 3))
            expected = np.rint((np.arange(48) + .5) / 48 * 255)
            np.testing.assert_allclose(silent[24, :, 0], expected, atol=2)
            np.testing.assert_allclose(silent[:, 24, 1], expected, atol=2)
            # projectM's normalized bands have a nonzero startup baseline.
            self.assertLess(int(np.ptp(silent[..., 2])), 2)
            samples = (.7 * np.sin(2 * np.pi * 110 * np.arange(4000) / 48000)).astype(np.float32)
            loud = renderer.render(samples, 0, 1)
            self.assertGreater(float(loud[..., 2].mean()), float(silent[..., 2].mean()) + 10)


if __name__ == "__main__":
    unittest.main()
