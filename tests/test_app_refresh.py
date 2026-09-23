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


class ReconnectServerTests(unittest.TestCase):
    def setUp(self):
        self.reconnect = threading.Event()
        self.server = PaletteServer(
            PaletteControls(), port=0,
            on_reconnect=self.reconnect.set,
            connection_state=lambda: {
                "status": "disconnected",
                "message": "USB disconnected · press Reconnect LEDs",
            },
        )
        self.server.start()
        self.addCleanup(self.server.close)

    def post(self, payload=b"{}", **headers):
        return urlopen(Request(
            self.server.url + "/api/reconnect", data=payload, method="POST",
            headers={"Content-Type": "application/json", **headers},
        ), timeout=2)

    def test_reconnect_reports_state_and_requests_a_serial_reset(self):
        with urlopen(self.server.url + "/api/state", timeout=2) as response:
            before = json.load(response)
        self.assertTrue(before["reconnect_available"])
        self.assertEqual(before["connection"]["status"], "disconnected")
        with self.post(Origin=self.server.url) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.load(response), {"reconnecting": True, "session": before["session"]})
        self.assertTrue(self.reconnect.wait(timeout=1))
        other = PaletteServer(PaletteControls(), port=0)
        other.start()
        self.addCleanup(other.close)
        with urlopen(other.url + "/api/state", timeout=2) as response:
            after = json.load(response)
        self.assertNotEqual(before["session"], after["session"])
        self.assertFalse(after["reconnect_available"])
        self.assertEqual(after["connection"]["status"], "preview")

    def test_invalid_or_cross_origin_requests_cannot_reset_the_connection(self):
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
                self.assertFalse(self.reconnect.is_set())


class ReconnectLifecycleTests(unittest.TestCase):
    def test_button_closes_stale_serial_and_retries_until_usb_returns(self):
        events = []
        capture = mock.Mock()
        capture.close.side_effect = lambda: events.append("audio closed")
        stale = mock.Mock()
        stale.close.side_effect = lambda: events.append("stale serial closed")
        reconnected = mock.Mock()
        reconnected.close.side_effect = lambda: events.append("new serial closed")
        server = mock.Mock(port=49123)
        server.close.side_effect = lambda: events.append("http closed")
        reconnect = None
        states = None

        def make_server(*args, on_reconnect, connection_state, **kwargs):
            nonlocal reconnect, states
            reconnect = on_reconnect
            states = connection_state
            return server

        stream_calls = 0

        def stream(connection, source, **kwargs):
            nonlocal stream_calls
            self.assertEqual(source.size, 48)
            self.assertEqual(kwargs["display_size"], 48)
            stream_calls += 1
            if stream_calls == 1:
                self.assertEqual(states()["status"], "live")
                reconnect()
                next(source.iter_frames())
            raise KeyboardInterrupt()

        with (
            mock.patch.object(app, "AudioCapture", return_value=capture),
            mock.patch.object(app, "PaletteServer", side_effect=make_server),
            mock.patch.object(
                app, "resolve_port",
                side_effect=["old-port", RuntimeError("USB is still absent"), "new-port"],
            ) as resolve,
            mock.patch.object(app, "open_serial", side_effect=[stale, reconnected]) as opened,
            mock.patch.object(app, "handshake"),
            mock.patch.object(app, "stream_frames", side_effect=stream),
            mock.patch.object(app.time, "sleep"),
        ):
            result = app.main([
                "--style", "spectrum", "--controls-port", "0", "--no-browser",
                "--slowdown", "50", "--fps", "60",
            ])
        self.assertEqual(result, 130)
        self.assertEqual(events, [
            "stale serial closed", "new serial closed", "http closed", "audio closed",
        ])
        self.assertEqual(resolve.call_args_list[0].args, ("auto",))
        self.assertEqual(resolve.call_args_list[0].kwargs["wait_timeout"], 30)
        self.assertEqual(resolve.call_args_list[1].kwargs["wait_timeout"], 1.0)
        self.assertEqual(resolve.call_args_list[2].kwargs["wait_timeout"], 1.0)
        self.assertEqual([call.args[0] for call in opened.call_args_list], ["old-port", "new-port"])
        self.assertEqual(states()["status"], "live")
        server.start.assert_called_once_with()

    def test_retry_times_out_and_a_second_click_starts_a_new_window(self):
        class ClickEvent:
            def __init__(self):
                self.flag = False
                self.waits = []

            def set(self):
                self.flag = True

            def is_set(self):
                return self.flag

            def clear(self):
                self.flag = False

            def wait(self, timeout=None):
                self.waits.append(timeout)
                if timeout is None:
                    self.flag = True  # Simulate the user clicking again after timeout.
                return self.flag

        click = ClickEvent()
        capture = mock.Mock()
        stale = mock.Mock()
        reconnected = mock.Mock()
        server = mock.Mock(port=49123)
        reconnect = None
        stream_calls = 0
        state = app.PanelConnectionState()
        updates = []
        original_update = state.update

        def update(status, message):
            updates.append((status, message))
            original_update(status, message)

        state.update = update

        def make_server(*args, on_reconnect, **kwargs):
            nonlocal reconnect
            reconnect = on_reconnect
            return server

        def stream(connection, source, **kwargs):
            nonlocal stream_calls
            stream_calls += 1
            if stream_calls == 1:
                reconnect()
                next(source.iter_frames())
            raise KeyboardInterrupt()

        with (
            mock.patch.object(app.threading, "Event", return_value=click),
            mock.patch.object(app, "PanelConnectionState", return_value=state),
            mock.patch.object(app, "AudioCapture", return_value=capture),
            mock.patch.object(app, "PaletteServer", side_effect=make_server),
            mock.patch.object(app, "resolve_port", side_effect=["old-port", "new-port"]),
            mock.patch.object(app, "open_serial", side_effect=[stale, reconnected]),
            mock.patch.object(app, "handshake"),
            mock.patch.object(app, "stream_frames", side_effect=stream),
            mock.patch.object(app.time, "sleep"),
            mock.patch.object(app.time, "monotonic", side_effect=[0.0, 61.0, 61.0]),
        ):
            result = app.main(["--controls-port", "0", "--no-browser"])

        self.assertEqual(result, 130)
        self.assertIn(
            ("disconnected", "Reconnect timed out · press Reconnect LEDs to try again"),
            updates,
        )
        self.assertIn(None, click.waits)
        self.assertEqual(stream_calls, 2)
        self.assertEqual(state.snapshot()["status"], "live")

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
                 mock.patch.object(launch_audio_visualizer.audio_service, "installed", return_value=False), \
                 mock.patch.object(launch_audio_visualizer.subprocess, "Popen", return_value=worker):
                self.assertEqual(launch_audio_visualizer.main(), 130)
            self.assertEqual(calls, 2)
            worker.terminate.assert_not_called()

    def test_dock_uses_installed_service_without_starting_competing_worker(self):
        with (
            mock.patch.object(launch_audio_visualizer.audio_service, "installed", return_value=True),
            mock.patch.object(launch_audio_visualizer.audio_service, "start") as start,
            mock.patch.object(launch_audio_visualizer.webbrowser, "open"),
            mock.patch.object(launch_audio_visualizer.subprocess, "Popen") as worker,
        ):
            self.assertEqual(launch_audio_visualizer.main(), 0)
            start.assert_called_once_with()
            worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
