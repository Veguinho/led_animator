import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audio_palette_controls import PaletteControls, PaletteServer
from video_controls import VideoPlaybackControls
from video_library import LocalVideoLibrary
import video_app


class VideoLibraryTests(unittest.TestCase):
    def test_preview_switch_releases_server_and_reuses_its_port_without_board(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.mp4"
            original.write_bytes(b"video")
            selected = Path(directory) / "selected.mp4"
            events = []
            server = mock.Mock(port=49123)
            server.close.side_effect = lambda: events.append("closed")

            def make_server(*args, on_video_start, **kwargs):
                server.start.side_effect = lambda: on_video_start(selected)
                return server

            with (
                mock.patch.object(video_app, "probe_video_duration", return_value=60),
                mock.patch.object(video_app, "probe_video", return_value=mock.Mock(fps=30)),
                mock.patch.object(video_app, "PaletteServer", side_effect=make_server),
                mock.patch.object(video_app.os, "execv", side_effect=lambda *args: events.append("exec")) as execute,
            ):
                result = video_app.main([str(original), "--no-browser", "--controls-port", "0", "--fps", "24"])
            self.assertEqual(result, 0)
            self.assertEqual(events, ["closed", "exec"])
            self.assertEqual(execute.call_args.args[1][2:],
                [str(selected), "--controls-port", "49123", "--no-browser", "--fps", "24.0"])

    def test_scans_local_sources_and_ignores_incomplete_and_external_files(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            names = ["original.mp4", "video_clips/set/clip.MP4", ".build/youtube/cached.mp4"]
            for name in names + [".build/youtube/download.f134.mp4", ".build/youtube/download.temp.mp4",
                                  ".build/youtube/download.mp4.part", "unrelated/hidden.mp4", "audio.mp3"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"media")
            (root / "empty.mp4").touch()
            external = Path(outside) / "private.mp4"
            external.write_bytes(b"external")
            (root / "video_clips/external.mp4").symlink_to(external)
            library = LocalVideoLibrary(root)
            self.assertEqual(set(library.videos()), set(names))
            self.assertEqual(next(iter(library.videos())), ".build/youtube/cached.mp4")

    def test_refresh_adds_and_removes_files_without_restarting_and_caches_polls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.mp4"
            first.write_bytes(b"first")
            library = LocalVideoLibrary(root, refresh_seconds=2)
            with mock.patch("video_library.time.monotonic", return_value=0) as clock:
                self.assertEqual(list(library.videos()), ["first.mp4"])
                second = root / "second.mp4"
                second.write_bytes(b"second")
                first.unlink()
                self.assertEqual(list(library.videos()), ["first.mp4"])
                clock.return_value = 3
                self.assertEqual(list(library.videos()), ["second.mp4"])
                second.unlink()
                self.assertEqual(library.videos(refresh=True), {})

    def test_running_video_can_select_newly_added_file_and_reject_removed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.mp4"
            first.write_bytes(b"first")
            library = LocalVideoLibrary(root, refresh_seconds=3600)
            started = threading.Event()
            callback = mock.Mock(side_effect=lambda path: started.set())
            with mock.patch("video_library.LocalVideoLibrary", return_value=library):
                server = PaletteServer(PaletteControls(), port=0,
                    video_controls=VideoPlaybackControls(first.name, 60, 30),
                    video_path=first, on_video_start=callback)
            server.start()

            def get(path):
                with urlopen(server.url + path, timeout=2) as response:
                    return json.load(response)

            def post(name):
                return urlopen(Request(server.url + "/api/video/start", method="POST",
                    data=json.dumps({"video": name}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=2)

            try:
                self.assertEqual(get("/api/state")["videos"], ["first.mp4"])
                second = root / "new.mp4"
                second.write_bytes(b"new")
                self.assertEqual(get("/api/video/library")["videos"], ["first.mp4", "new.mp4"])
                first.unlink()
                with self.assertRaises(HTTPError) as error:
                    post("first.mp4")
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                with post("new.mp4") as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(started.wait(1))
                callback.assert_called_once_with(second.resolve())
            finally:
                server.close()
