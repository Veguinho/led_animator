import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audio_palette_controls import PaletteControls, PaletteServer
import system_audio_visualizer as app
import stream_arduino
import led_animator


class VideoLaunchTests(unittest.TestCase):
    def test_media_tools_are_resolved_with_the_macos_service_path(self):
        for tool in ("ffprobe", "ffmpeg"):
            with (
                self.subTest(tool=tool),
                mock.patch.object(led_animator.sys, "platform", "darwin"),
                mock.patch.object(led_animator.shutil, "which",
                                  side_effect=[None, f"/usr/local/bin/{tool}"]) as which,
            ):
                self.assertEqual(led_animator._require_program(tool), f"/usr/local/bin/{tool}")
                self.assertEqual(which.call_args_list,
                    [mock.call(tool), mock.call(tool, path="/opt/homebrew/bin:/usr/local/bin")])

    def test_duration_probe_uses_the_resolved_executable(self):
        with (
            mock.patch.object(stream_arduino, "_require_program", return_value="/usr/local/bin/ffprobe"),
            mock.patch.object(stream_arduino.subprocess, "run",
                              return_value=mock.Mock(stdout='{"format":{"duration":"12.5"}}')) as run,
        ):
            self.assertEqual(stream_arduino.probe_video_duration(Path("clip.mp4")), 12.5)
        self.assertEqual(run.call_args.args[0][0], "/usr/local/bin/ffprobe")

    def test_launch_accepts_only_library_files_and_prevents_duplicate_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "clip.mp4"
            clip.touch()
            started = threading.Event()
            callback = mock.Mock(side_effect=lambda path: started.set())
            server = PaletteServer(PaletteControls(), port=0, on_video_start=callback,
                                   video_library={"clip.mp4": clip})
            server.start()
            try:
                with urlopen(server.url + "/api/state") as response:
                    state = json.load(response)
                self.assertTrue(state["video_start_available"])
                self.assertEqual(state["videos"], ["clip.mp4"])
                self.assertEqual(state["mode"], "audio")

                def post(payload, **headers):
                    return urlopen(Request(server.url + "/api/video/start",
                        data=json.dumps(payload).encode(), method="POST",
                        headers={"Content-Type": "application/json", **headers}), timeout=2)

                for payload, headers, code in [
                    ({"video": "clip.mp4"}, {"Origin": "https://example.com"}, 403),
                    ({"video": "../clip.mp4"}, {}, 400),
                    ({"video": ["clip.mp4"]}, {}, 400),
                    ({"video": "missing.mp4"}, {}, 400),
                    ({"video": "clip.mp4", "command": "start"}, {}, 400),
                    (None, {}, 400),
                ]:
                    with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                        post(payload, **headers)
                    self.assertEqual(error.exception.code, code)
                    error.exception.close()
                callback.assert_not_called()
                with post({"video": "clip.mp4"}, Origin=server.url) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)["session"], state["session"])
                self.assertTrue(started.wait(1))
                callback.assert_called_once_with(clip)
                with self.assertRaises(HTTPError) as error:
                    post({"video": "clip.mp4"})
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
                callback.assert_called_once()
            finally:
                server.close()

    def test_switch_closes_connections_before_launching_video_on_the_same_port(self):
        events = []
        capture = mock.Mock()
        capture.close.side_effect = lambda: events.append("audio closed")
        connection = mock.Mock()
        connection.close.side_effect = lambda: events.append("serial closed")
        server = mock.Mock(port=49123)
        server.close.side_effect = lambda: events.append("http closed")
        callback = None

        def make_server(*args, on_video_start, **kwargs):
            nonlocal callback
            callback = on_video_start
            return server

        def stream(connection, source, **kwargs):
            callback(Path("/tmp/selected clip.mp4"))
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
            result = app.main(["--controls-port", "0", "--no-browser", "--clear-on-exit"])
        self.assertEqual(result, 0)
        self.assertEqual(events, ["http closed", "audio closed", "panel cleared", "serial closed", "exec"])
        executable, argv = execute.call_args.args
        self.assertEqual(executable, app.sys.executable)
        self.assertEqual(argv[1], str(Path(stream_arduino.__file__).resolve()))
        options = stream_arduino.build_parser().parse_args(argv[2:])
        self.assertEqual(options.video, Path("/tmp/selected clip.mp4"))
        self.assertEqual(options.port, "test-port")
        self.assertEqual(options.controls_port, 49123)
        self.assertEqual(options.display_size, 48)
        self.assertIsNone(options.fps)
        self.assertTrue(options.no_browser)
        self.assertTrue(options.loop)
        self.assertTrue(options.clear_on_exit)


if __name__ == "__main__":
    unittest.main()
