import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_reload import RELOAD_STATE_KEY, save_reload_state, take_reload_state
from audio_palette_controls import PaletteControls, PaletteServer, default_settings
from board_controls import BoardResetControls
import audio_palette_controls
import stream_arduino as stream
import system_audio_visualizer as audio
from video_controls import VideoPlaybackControls


class SystemResetServerTests(unittest.TestCase):
    def start_server(self, *, video=None, enabled=True):
        boards = BoardResetControls()
        reloaded = threading.Event()
        server = PaletteServer(
            PaletteControls(), port=0, video_controls=video, board_controls=boards,
            on_reset=(lambda: boards.request(on_complete=reloaded.set)) if enabled else None,
        )
        server.start()
        self.addCleanup(server.close)
        return server, boards, reloaded

    def post(self, server, payload=b"{}", **headers):
        return urlopen(Request(server.url + "/api/reset", data=payload, method="POST",
                              headers={"Content-Type": "application/json", **headers}), timeout=2)

    def test_button_queues_hardware_first_and_reloads_only_after_success(self):
        for video in (None, VideoPlaybackControls("clip.mp4", 60, 30)):
            with self.subTest(video=video):
                server, boards, reloaded = self.start_server(video=video)
                with urlopen(server.url) as response:
                    self.assertIn(b"Reset &amp; reload", response.read())
                with urlopen(server.url + "/api/state") as response:
                    before = json.load(response)
                self.assertTrue(before["system_reset_available"])
                for _ in range(2):
                    with self.post(server, Origin=server.url) as response:
                        result = json.load(response)
                        self.assertEqual(response.status, 202)
                        self.assertEqual(result["session"], before["session"])
                        self.assertEqual(result["board_reset"]["revision"], 1)
                self.assertFalse(reloaded.is_set())
                boards.process(lambda: self.assertFalse(reloaded.is_set()))
                self.assertTrue(reloaded.is_set())

    def test_failed_hardware_reset_does_not_restart_software_and_can_be_retried(self):
        server, boards, reloaded = self.start_server()
        with self.post(server):
            pass
        boards.process(mock.Mock(side_effect=RuntimeError("no controller response")))
        self.assertFalse(reloaded.is_set())
        self.assertEqual(boards.state()["error"], "no controller response")
        # Retrying the board-only action must not trigger the failed reload.
        boards.request()
        boards.process(lambda: None)
        self.assertFalse(reloaded.is_set())
        with self.post(server):
            pass
        boards.process(lambda: None)
        self.assertTrue(reloaded.is_set())

    def test_pending_board_only_reset_can_be_upgraded_to_a_full_reload(self):
        server, boards, reloaded = self.start_server()
        boards.request()
        with self.post(server):
            pass
        boards.process(lambda: None)
        self.assertTrue(reloaded.is_set())
        self.assertEqual(boards.state()["revision"], 1)

    def test_invalid_requests_and_previews_cannot_reset_the_system(self):
        server, boards, reloaded = self.start_server()
        for payload, headers, code in (
            (b"null", {}, 400), (b'{"reset":true}', {}, 400),
            (b"{}", {"Origin": "https://example.com"}, 403),
            (b"{}", {"Host": "example.com"}, 403),
            (b"{}", {"Content-Type": "text/plain"}, 400),
        ):
            with self.subTest(payload=payload, headers=headers), self.assertRaises(HTTPError) as error:
                self.post(server, payload, **headers)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
        self.assertEqual(boards.state()["status"], "idle")
        self.assertFalse(reloaded.is_set())
        preview, _, _ = self.start_server(enabled=False)
        with self.assertRaises(HTTPError) as error:
            self.post(preview)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()


class SystemResetLifecycleTests(unittest.TestCase):
    def test_audio_reset_closes_resources_then_execs_with_current_palette_and_port(self):
        events = []
        palette = PaletteControls(size=48)
        palette.update(default_settings() | {"preset": "ocean", "brightness": .25})
        capture, connection, server = mock.Mock(), mock.Mock(), mock.Mock(port=49123)
        capture.close.side_effect = lambda: events.append("audio closed")
        connection.close.side_effect = lambda: events.append("serial closed")
        server.close.side_effect = lambda: events.append("http closed")
        reset = None

        def make_server(*args, on_reset, **kwargs):
            nonlocal reset
            reset = on_reset
            return server

        def run_frames(connection, source, **kwargs):
            reset()
            kwargs["board_controls"].process(lambda: events.append("hardware reset"))
            next(source.iter_frames())

        with (
            mock.patch.dict(os.environ),
            mock.patch.object(audio, "PaletteControls", return_value=palette),
            mock.patch.object(audio, "AudioCapture", return_value=capture),
            mock.patch.object(audio, "PaletteServer", side_effect=make_server),
            mock.patch.object(audio, "resolve_port", return_value="test-port"),
            mock.patch.object(audio, "open_serial", return_value=connection),
            mock.patch.object(audio, "handshake"),
            mock.patch.object(audio, "stream_frames", side_effect=run_frames),
            mock.patch.object(audio.time, "sleep"),
            mock.patch.object(audio.os, "execv", side_effect=lambda *a: events.append("exec")) as execute,
        ):
            os.environ.pop(RELOAD_STATE_KEY, None)
            result = audio.main(["--no-browser", "--controls-port", "0", "--fps", "30"])
            state = take_reload_state()
        self.assertEqual(result, 0)
        self.assertEqual(events, ["hardware reset", "http closed", "audio closed", "serial closed", "exec"])
        self.assertEqual(state["palette"], palette.settings_snapshot()[1])
        restarted = audio.build_parser().parse_args(execute.call_args.args[1][2:])
        self.assertEqual(restarted.controls_port, 49123)
        self.assertEqual(restarted.port, "test-port")
        self.assertEqual(restarted.fps, 30)
        self.assertTrue(restarted.no_browser)

    def test_video_reset_preserves_position_pause_layers_palette_and_original_options(self):
        events = []
        palette = PaletteControls(size=48)
        palette.update(default_settings() | {"preset": "sunset", "brightness": .45})
        connection, server = mock.Mock(), mock.Mock(port=49123)
        connection.close.side_effect = lambda: events.append("serial closed")
        server.close.side_effect = lambda: events.append("http closed")
        reset = None

        def make_server(*args, on_reset, **kwargs):
            nonlocal reset
            reset = on_reset
            return server

        def run_frames(connection, source, **kwargs):
            video = kwargs["controls"]
            video.set_position(12.5)
            video.set_paused(True)
            video.set_layers({"video": False, "audio_palette": False})
            reset()
            self.assertFalse(kwargs["should_stop"]())
            kwargs["board_controls"].process(lambda: events.append("hardware reset"))
            self.assertTrue(kwargs["should_stop"]())
            return 1, 0

        with (
            mock.patch.dict(os.environ),
            mock.patch.object(audio_palette_controls, "PaletteControls", return_value=palette),
            mock.patch.object(stream, "probe_video_duration", return_value=60),
            mock.patch.object(stream, "probe_video", return_value=mock.Mock(fps=30)),
            mock.patch.object(stream, "open_video", return_value=stream.FrameSource(30, lambda: iter(()))),
            mock.patch.object(audio_palette_controls, "PaletteServer", side_effect=make_server),
            mock.patch.object(stream, "resolve_port", return_value="test-port"),
            mock.patch.object(stream, "open_serial", return_value=connection),
            mock.patch.object(stream, "handshake"),
            mock.patch.object(stream, "stream_frames", side_effect=run_frames),
            mock.patch.object(stream.time, "sleep"),
            mock.patch.object(stream.os, "execv", side_effect=lambda *a: events.append("exec")) as execute,
        ):
            os.environ.pop(RELOAD_STATE_KEY, None)
            result = stream.main(["clip.mp4", "--no-browser", "--controls-port", "0",
                                  "--no-audio-reactive", "--audio-response", "smooth", "--start", "3"])
            state = take_reload_state()
        self.assertEqual(result, 0)
        self.assertEqual(events, ["hardware reset", "http closed", "serial closed", "exec"])
        self.assertEqual(state["palette"], palette.settings_snapshot()[1])
        self.assertEqual(state["video"], {"paused": True, "layers": {"video": False, "audio_palette": False}})
        restarted = stream.build_parser().parse_args(execute.call_args.args[1][2:])
        self.assertEqual(restarted.video, Path("clip.mp4"))
        self.assertEqual(restarted.start, 12.5)
        self.assertEqual(restarted.controls_port, 49123)
        self.assertEqual(restarted.audio_response, "smooth")
        self.assertEqual(restarted.port, "test-port")
        self.assertTrue(restarted.no_audio_reactive)
        self.assertFalse(restarted.loop)

    def test_reloaded_video_restores_controls_before_decoding(self):
        palette = PaletteControls(size=48)
        settings = default_settings() | {"preset": "ocean", "brightness": .23}

        def open_video(*args, texture, **kwargs):
            self.assertTrue(texture.video_controls.is_paused())
            self.assertEqual(texture.video_controls.output_layers(), {"video": False, "audio_palette": False})
            self.assertEqual(texture.controls.settings_snapshot()[1], palette.settings_snapshot()[1])
            self.assertEqual(palette.settings_snapshot()[1]["brightness"], .23)
            return stream.FrameSource(30, lambda: iter(()))

        with (
            mock.patch.dict(os.environ),
            mock.patch.object(audio_palette_controls, "PaletteControls", return_value=palette),
            mock.patch.object(stream, "probe_video_duration", return_value=60),
            mock.patch.object(stream, "probe_video", return_value=mock.Mock(fps=30)),
            mock.patch.object(stream, "open_video", side_effect=open_video),
            mock.patch.object(audio_palette_controls, "PaletteServer"),
            mock.patch.object(stream, "resolve_port", return_value="test-port"),
            mock.patch.object(stream, "open_serial"),
            mock.patch.object(stream, "handshake"),
            mock.patch.object(stream, "stream_frames", return_value=(1, 0)),
            mock.patch.object(stream.time, "sleep"),
        ):
            save_reload_state(settings, {"paused": True, "layers": {"video": False, "audio_palette": False}})
            self.assertEqual(stream.main(["clip.mp4", "--no-browser", "--no-audio-reactive"]), 0)
            self.assertEqual(take_reload_state(), {})


if __name__ == "__main__":
    unittest.main()
