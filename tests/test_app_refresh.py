import fcntl
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audio_palette_controls import PaletteControls, PaletteServer
from scripts import launch_audio_visualizer
import system_audio_visualizer as app


class RefreshServerTests(unittest.TestCase):
    def setUp(self):
        self.refresh = threading.Event()
        self.server = PaletteServer(PaletteControls(), port=0, on_refresh=self.refresh.set)
        self.server.start()
        self.addCleanup(self.server.close)

    def post(self, payload=b"{}", **headers):
        return urlopen(Request(
            self.server.url + "/api/refresh", data=payload, method="POST",
            headers={"Content-Type": "application/json", **headers},
        ), timeout=2)

    def test_refresh_acknowledges_the_current_session_and_requests_restart(self):
        with urlopen(self.server.url + "/api/state", timeout=2) as response:
            before = json.load(response)
        self.assertTrue(before["refresh_available"])
        with self.post(Origin=self.server.url) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response), {"restarting": True, "session": before["session"]})
        self.assertTrue(self.refresh.wait(timeout=1))
        other = PaletteServer(PaletteControls(), port=0)
        other.start()
        self.addCleanup(other.close)
        with urlopen(other.url + "/api/state", timeout=2) as response:
            after = json.load(response)
        self.assertNotEqual(before["session"], after["session"])
        self.assertFalse(after["refresh_available"])

    def test_invalid_or_cross_origin_requests_cannot_restart_the_app(self):
        for payload, headers, code in [
            (b"{}", {"Origin": "https://example.com"}, 403),
            (b"{}", {"Host": "example.com"}, 403),
            (b"null", {}, 400), (b"{", {}, 400),
            (b'{"command":"restart"}', {}, 400),
        ]:
            with self.subTest(payload=payload, headers=headers):
                with self.assertRaises(HTTPError) as error:
                    self.post(payload, **headers)
                self.assertEqual(error.exception.code, code)
                error.exception.close()
                self.assertFalse(self.refresh.is_set())


class RefreshLifecycleTests(unittest.TestCase):
    def test_restart_closes_resources_and_preserves_cli_and_browser_port(self):
        events = []
        capture = mock.Mock()
        capture.close.side_effect = lambda: events.append("audio closed")
        connection = mock.Mock()
        connection.close.side_effect = lambda: events.append("serial closed")
        server = mock.Mock(port=49123)
        server.close.side_effect = lambda: events.append("http closed")
        refresh = None

        def make_server(*args, on_refresh, **kwargs):
            nonlocal refresh
            refresh = on_refresh
            return server

        def stream(connection, source, **kwargs):
            refresh()
            next(source.iter_frames())

        with (
            mock.patch.object(app, "AudioCapture", return_value=capture),
            mock.patch.object(app, "PaletteServer", side_effect=make_server),
            mock.patch.object(app, "resolve_port", return_value="test-port"),
            mock.patch.object(app, "open_serial", return_value=connection),
            mock.patch.object(app, "handshake"),
            mock.patch.object(app, "exchange_packet", side_effect=lambda *args: events.append("panel cleared")),
            mock.patch.object(app, "stream_frames", side_effect=stream),
            mock.patch.object(app.time, "sleep"),
            mock.patch.object(app.os, "execv", side_effect=lambda *args: events.append("exec")) as execute,
        ):
            result = app.main(["--style", "spectrum", "--controls-port", "0", "--no-browser",
                               "--slowdown", "50", "--fps", "60", "--clear-on-exit"])
        self.assertEqual(result, 0)
        self.assertEqual(events, ["http closed", "audio closed", "panel cleared", "serial closed", "exec"])
        executable, argv = execute.call_args.args
        self.assertEqual(executable, app.sys.executable)
        self.assertEqual(argv[1], str(Path(app.__file__).resolve()))
        restarted = app.build_parser().parse_args(argv[2:])
        self.assertEqual((restarted.style, restarted.fps, restarted.slowdown), ("spectrum", 60, 50))
        self.assertEqual(restarted.controls_port, 49123)
        self.assertTrue(restarted.no_browser)
        self.assertTrue(restarted.clear_on_exit)

    def test_launcher_keeps_its_lock_while_worker_runs_and_shuts_down(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = 0

            def wait():
                nonlocal calls
                with (root / ".build" / "desktop-app.lock").open("a") as competing:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt()
                return 130

            worker = mock.MagicMock()
            worker.__enter__.return_value = worker
            worker.wait.side_effect = wait
            with mock.patch.object(launch_audio_visualizer, "ROOT", root), \
                 mock.patch.object(launch_audio_visualizer.subprocess, "Popen", return_value=worker):
                self.assertEqual(launch_audio_visualizer.main(), 130)
            self.assertEqual(calls, 2)
            worker.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
