import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.request import Request, urlopen

from mp4_player import stream as mp4_stream
from mp4_player.playback_controls import PlaybackControls, PlaybackControlServer
from mp4_player.prepare import DISPLAY_FILTER, enhance_prepared_video, prepared_path


class VideoPlaybackControlTests(unittest.TestCase):
    def setUp(self):
        self.controls = PlaybackControls()
        self.server = PlaybackControlServer(self.controls, port=0)
        self.server.start()
        self.addCleanup(self.server.close)

    def request(self, path: str, method: str = "GET") -> dict:
        request = Request(
            self.server.url.removesuffix("/video") + path,
            method=method,
            headers={"Origin": self.server.url.removesuffix("/video")},
        )
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_page_only_exposes_play_and_pause_actions(self):
        with urlopen(self.server.url, timeout=2) as response:
            page = response.read().decode()
        self.assertIn('id="play"', page)
        self.assertIn('id="pause"', page)
        self.assertNotIn('type="range"', page)

    def test_pause_and_play_change_shared_stream_state(self):
        self.assertTrue(self.request("/api/video/state")["playing"])
        self.assertFalse(self.request("/api/video/pause", "POST")["playing"])
        self.assertTrue(self.controls.paused())
        self.assertTrue(self.request("/api/video/play", "POST")["playing"])
        self.assertFalse(self.controls.paused())

    def test_connection_state_is_reported_without_adding_controls(self):
        self.controls.set_connection("live")
        self.assertEqual(self.request("/api/video/state")["connection"], "live")
        self.controls.set_connection("reconnecting")
        self.assertEqual(self.request("/api/video/state")["connection"], "reconnecting")

    def test_prepared_cache_changes_with_source_or_fps(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "movie.mp4"
            source.write_bytes(b"first")
            first = prepared_path(source, 6, root / "cache")
            self.assertEqual(first, prepared_path(source, 6.0, root / "cache"))
            second = prepared_path(source, 10, root / "cache")
            source.write_bytes(b"a changed source")
            changed = prepared_path(source, 6, root / "cache")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_high_dynamic_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "prepared.mp4"
            source.write_bytes(b"prepared")
            expected = source.with_name(
                "prepared-high-dynamic-" + hashlib.sha256(
                    f"{source.resolve()}|{source.stat().st_size}|{source.stat().st_mtime_ns}|luma-s-curve-sat-v3".encode()
                ).hexdigest()[:12] + ".mp4"
            )
            expected.write_bytes(b"enhanced")
            self.assertEqual(enhance_prepared_video(source), expected)

    def test_display_filter_uses_a_highlight_lifting_s_curve(self):
        self.assertIn("0.50/0.42", DISPLAY_FILTER)
        self.assertIn("0.75/0.84", DISPLAY_FILTER)
        self.assertIn("0.90/0.98", DISPLAY_FILTER)
        self.assertIn("1/1", DISPLAY_FILTER)
        self.assertIn("saturation=1.18", DISPLAY_FILTER)

    def test_reconnect_resumes_after_the_last_acknowledged_frame(self):
        attempts = 0

        def run(_arguments, **callbacks):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                callbacks["progress_callback"](437)
                return 1
            return 0

        with (
            mock.patch.object(mp4_stream.stream_arduino, "main", side_effect=run) as main,
            mock.patch.object(mp4_stream.time, "sleep"),
        ):
            result = mp4_stream.stream_with_resume(["prepared.mp4"], self.controls)

        self.assertEqual(result, 0)
        self.assertEqual(main.call_args_list[0].args[0][-2:], ["--start-frame", "0"])
        self.assertEqual(main.call_args_list[1].args[0][-2:], ["--start-frame", "437"])


if __name__ == "__main__":
    unittest.main()
