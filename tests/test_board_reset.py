import json
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audio_palette_controls import PaletteControls, PaletteServer
from board_controls import BoardResetControls
import stream_arduino as stream
from video_controls import VideoPlaybackControls


class BoardResetTests(unittest.TestCase):
    def test_reset_releases_reset_line_and_restores_video_handshake(self):
        events = []

        class Connection:
            def __setattr__(self, name, value):
                events.append((name, value))

            def reset_input_buffer(self):
                events.append("drained")

        with (
            mock.patch.object(stream.time, "sleep", side_effect=lambda duration: events.append(duration)),
            mock.patch.object(stream, "handshake") as handshake,
        ):
            stream.reset_board(Connection(), 30, .2, 0, 48, video_mode=True)
        self.assertEqual(events, [("dtr", False), ("rts", True), .1,
                                  ("rts", False), .5, "drained"])
        handshake.assert_called_once_with(mock.ANY, 30, .2, 3, 48, video_mode=True)

    def test_reset_line_is_released_even_when_reset_is_interrupted(self):
        connection = mock.Mock()
        with mock.patch.object(stream.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                stream.reset_board(connection, 12, 1, 3, 48)
        self.assertFalse(connection.rts)
        connection.reset_input_buffer.assert_not_called()

    def test_requests_are_coalesced_and_failures_can_be_retried(self):
        boards = BoardResetControls()
        reset = mock.Mock(side_effect=RuntimeError("controller did not respond"))
        boards.request()
        boards.request()
        self.assertTrue(boards.process(reset))
        self.assertFalse(boards.process(reset))
        reset.assert_called_once_with()
        self.assertEqual(boards.state(), {"status": "error", "error": "controller did not respond", "revision": 1})
        boards.request()
        self.assertIsNone(boards.state()["error"])
        self.assertTrue(boards.process(lambda: None))
        self.assertEqual(boards.state()["status"], "ready")
        self.assertEqual(boards.state()["revision"], 2)

    def test_reset_between_frames_preserves_video_timeline_and_settings(self):
        boards = BoardResetControls()
        video = VideoPlaybackControls("clip.mp4", 60, 10)
        now = [0.0]
        sent = []
        source = stream.FrameSource(10, lambda: iter([bytes([i]) * 512 for i in (1, 2, 3)]))

        def exchange(connection, kind, sequence, payload, *args):
            sent.append((payload[0], now[0]))
            if len(sent) == 1:
                boards.request()

        def reset(*args, **kwargs):
            self.assertEqual(len(sent), 1)
            now[0] += 2

        with (
            mock.patch.object(stream.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(stream.time, "sleep", side_effect=lambda dt: now.__setitem__(0, now[0] + dt)),
            mock.patch.object(stream, "exchange_packet", side_effect=exchange),
            mock.patch.object(stream, "reset_board", side_effect=reset) as reboot,
            mock.patch.object(video, "set_position", wraps=video.set_position) as positions,
        ):
            result = stream.stream_frames(mock.Mock(), source, loop=False, timeout=1,
                retries=0, drop_late=True, controls=video, board_controls=boards,
                video_mode=True)
        self.assertEqual(result, (3, 0))
        self.assertEqual([value for value, _ in sent], [1, 2, 3])
        self.assertAlmostEqual(sent[1][1], 2.1)
        self.assertAlmostEqual(sent[2][1], 2.2)
        self.assertEqual([call.args[0] for call in positions.call_args_list[:3]], [0, .1, .2])
        self.assertEqual(boards.state()["status"], "ready")
        self.assertTrue(reboot.call_args.kwargs["video_mode"])

    def test_reset_while_paused_restores_the_frame_and_resume_timing(self):
        boards = BoardResetControls()
        video = VideoPlaybackControls("clip.mp4", 60, 10)
        now = [0.0]
        sent = []
        source = stream.FrameSource(10, lambda: iter([bytes([i]) * 512 for i in (1, 2, 3)]))

        def exchange(connection, kind, sequence, payload, *args):
            sent.append((payload[0], now[0]))
            if len(sent) == 1:
                video.set_paused(True)

        def sleep(duration):
            now[0] += duration
            if video.is_paused() and boards.state()["status"] == "idle":
                boards.request()
            if len(sent) == 2:
                video.set_paused(False)

        with (
            mock.patch.object(stream.time, "monotonic", side_effect=lambda: now[0]),
            mock.patch.object(stream.time, "sleep", side_effect=sleep),
            mock.patch.object(stream, "exchange_packet", side_effect=exchange),
            mock.patch.object(stream, "reset_board", side_effect=lambda *a, **kw: now.__setitem__(0, now[0] + 2)),
        ):
            result = stream.stream_frames(mock.Mock(), source, loop=False, timeout=1,
                retries=0, drop_late=True, controls=video, board_controls=boards)
        self.assertEqual(result, (4, 0))
        self.assertEqual([value for value, _ in sent], [1, 1, 2, 3])
        self.assertAlmostEqual(sent[2][1] - sent[1][1], .2)
        self.assertAlmostEqual(sent[3][1] - sent[2][1], .1)


class BoardResetServerTests(unittest.TestCase):
    def start_server(self, boards=None, video=None):
        server = PaletteServer(PaletteControls(), port=0, board_controls=boards,
                               video_controls=video)
        server.start()
        self.addCleanup(server.close)
        return server

    def post(self, server, body=b"{}", **headers):
        return urlopen(Request(server.url + "/api/boards/reset", data=body,
            method="POST", headers={"Content-Type": "application/json", **headers}), timeout=2)

    def test_button_queues_reset_in_audio_and_video_without_changing_settings(self):
        for video in (None, VideoPlaybackControls("clip.mp4", 60, 30)):
            with self.subTest(video=video):
                boards = BoardResetControls()
                server = self.start_server(boards, video)
                with urlopen(server.url) as response:
                    self.assertIn(b'id="reset-boards"', response.read())
                with urlopen(server.url + "/api/state") as response:
                    before = json.load(response)
                self.assertTrue(before["board_reset_available"])
                with self.post(server, Origin=server.url) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)["board_reset"]["status"], "pending")
                with urlopen(server.url + "/api/state") as response:
                    after = json.load(response)
                self.assertEqual(after["settings"], before["settings"])
                self.assertEqual(after["video"], before["video"])

    def test_preview_and_invalid_requests_cannot_reset_hardware(self):
        boards = BoardResetControls()
        server = self.start_server(boards)
        for body, headers, expected in (
            (b"null", {}, 400), (b"{", {}, 400),
            (b'{"reset": true}', {}, 400),
            (b"{}", {"Origin": "https://example.com"}, 403),
            (b"{}", {"Host": "example.com"}, 403),
            (b"{}", {"Content-Type": "text/plain"}, 400),
        ):
            with self.subTest(body=body, headers=headers), self.assertRaises(HTTPError) as error:
                self.post(server, body, **headers)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
            self.assertEqual(boards.state()["status"], "idle")
        preview = self.start_server()
        with self.assertRaises(HTTPError) as error:
            self.post(preview)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        with urlopen(preview.url + "/api/state") as response:
            self.assertFalse(json.load(response)["board_reset_available"])


if __name__ == "__main__":
    unittest.main()
