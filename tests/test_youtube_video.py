import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from audio_palette_controls import PaletteControls, PaletteServer
from video_controls import VideoPlaybackControls
import stream_arduino
from app_reload import take_reload_state
from youtube_video import download_youtube, youtube_url


URL = "https://www.youtube.com/watch?v=pUWCnazcsbU"


class YouTubeTests(unittest.TestCase):
    def test_links_normalize_and_cannot_select_another_site_or_playlist(self):
        for value in (URL + "&list=anything&themeRefresh=1", "https://youtu.be/pUWCnazcsbU?t=30",
                      "https://m.youtube.com/shorts/pUWCnazcsbU", "https://youtube.com/live/pUWCnazcsbU"):
            self.assertEqual(youtube_url(value), URL)
        for value in (None, [], "file:///tmp/a.mp4", "https://youtube.com.evil.test/watch?v=pUWCnazcsbU",
                      "https://youtube.com/playlist?list=abc", "https://youtube.com/watch?v=../tmp/file",
                      "https://youtube.com@127.0.0.1/watch?v=pUWCnazcsbU"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                youtube_url(value)

    def test_cache_skips_network_and_empty_files_are_not_cache_hits(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "pUWCnazcsbU.mp4"
            cached.write_bytes(b"complete mp4")
            with mock.patch("yt_dlp.YoutubeDL") as downloader:
                self.assertEqual(download_youtube(URL, cache_dir=Path(directory)), cached)
                downloader.assert_not_called()

    def test_download_caps_resolution_and_rejects_live_before_downloading(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("yt_dlp.YoutubeDL") as factory:
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = {"id": "pUWCnazcsbU", "duration": 20,
                                                    "title": "Artist / Set: Live?"}
            destination = Path(directory) / "pUWCnazcsbU.mp4"
            downloader.process_info.side_effect = lambda info: destination.write_bytes(b"mp4")
            with mock.patch("youtube_video._require_program", return_value="/bin/ffmpeg"):
                named = Path(directory) / "Artist Set Live [pUWCnazcsbU].mp4"
                self.assertEqual(download_youtube(URL, cache_dir=Path(directory)), named)
                self.assertEqual(named.read_bytes(), b"mp4")
                options = factory.call_args.args[0]
                self.assertTrue(all("height<=360" in part for part in options["format"].split("/")))
                downloader.extract_info.assert_called_once_with(URL, download=False)
                named.unlink()
                downloader.process_info.reset_mock()
                downloader.extract_info.return_value = {"is_live": True, "duration": 20}
                with self.assertRaisesRegex(RuntimeError, "ao vivo"):
                    download_youtube(URL, cache_dir=Path(directory))
                downloader.process_info.assert_not_called()

    def test_titled_download_is_reused_without_fetching_or_downloading_again(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "Recognizable video title [pUWCnazcsbU].mp4"
            cached.write_bytes(b"complete mp4")
            with mock.patch("yt_dlp.YoutubeDL") as downloader:
                self.assertEqual(download_youtube(URL, cache_dir=Path(directory)), cached)
                downloader.assert_not_called()

    def test_server_downloads_in_background_blocks_duplicates_and_recovers_from_failure(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        attempts = []

        def download(url, *, progress):
            attempts.append(url)
            progress("Downloading…")
            entered.set()
            release.wait(2)
            if len(attempts) == 1:
                raise RuntimeError("network failed")
            return Path("/tmp/clip.mp4")

        server = PaletteServer(PaletteControls(), port=0,
                               on_video_start=lambda path: started.set())
        server.start()
        def state():
            with urlopen(server.url + "/api/state", timeout=2) as response:
                return json.load(response)

        def post(payload, **headers):
            return urlopen(Request(server.url + "/api/youtube/start", method="POST",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}), timeout=2)

        try:
            with mock.patch("youtube_video.download_youtube", side_effect=download):
                for payload, headers, code in [({"url": URL}, {"Origin": "https://evil.test"}, 403),
                                                ({"url": "https://example.com"}, {}, 400),
                                                ({"url": URL, "command": "anything"}, {}, 400)]:
                    with self.assertRaises(HTTPError) as error:
                        post(payload, **headers)
                    self.assertEqual(error.exception.code, code)
                    error.exception.close()
                with post({"url": URL}) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(entered.wait(1))
                self.assertEqual(state()["youtube"]["status"], "downloading")
                with self.assertRaises(HTTPError) as error:
                    post({"url": URL})
                self.assertEqual(error.exception.code, 409)
                error.exception.close()
                release.set()
                deadline = time.monotonic() + 2
                while state()["video_starting"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(state()["youtube"]["status"], "error")
                with post({"url": URL}) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(started.wait(1))
        finally:
            release.set()
            server.close()

    def test_switching_while_paused_closes_the_decoder(self):
        controls = VideoPlaybackControls("clip.mp4", 10, 30)
        controls.set_paused(True)
        stop = threading.Event()
        closed = []

        def frames():
            try:
                yield bytes(512)
            finally:
                closed.append(True)

        source = stream_arduino.FrameSource(30, frames)
        with mock.patch.object(stream_arduino.time, "sleep", side_effect=lambda _: stop.set()):
            result = stream_arduino.stream_frames(mock.Mock(), source, loop=True, timeout=1,
                retries=0, drop_late=False, controls=controls, should_stop=stop.is_set)
        self.assertEqual(result, (0, 0))
        self.assertEqual(closed, [True])

    def test_video_to_youtube_switch_closes_resources_and_keeps_playback_options(self):
        events = []
        connection = mock.Mock()
        connection.close.side_effect = lambda: events.append("serial closed")
        server = mock.Mock(port=49123)
        server.close.side_effect = lambda: events.append("http closed")
        callback = None

        def make_server(*args, on_video_start, **kwargs):
            nonlocal callback
            callback = on_video_start
            return server

        def stream(*args, **kwargs):
            kwargs["controls"].set_layers({"video": False, "audio_palette": False})
            kwargs["controls"].set_paused(True)
            callback(Path("/tmp/youtube.mp4"))
            self.assertTrue(kwargs["should_stop"]())
            return 0, 0

        with (
            mock.patch.dict(stream_arduino.os.environ),
            mock.patch.object(stream_arduino, "probe_video_duration", return_value=60),
            mock.patch.object(stream_arduino, "probe_video", return_value=mock.Mock(fps=30)),
            mock.patch.object(stream_arduino, "open_video", return_value=mock.Mock(fps=24)),
            mock.patch.object(stream_arduino, "resolve_port", return_value="test-port"),
            mock.patch.object(stream_arduino, "open_serial", return_value=connection),
            mock.patch.object(stream_arduino, "handshake"),
            mock.patch.object(stream_arduino, "stream_frames", side_effect=stream),
            mock.patch.object(stream_arduino.time, "sleep"),
            mock.patch("audio_palette_controls.PaletteServer", side_effect=make_server),
            mock.patch.object(stream_arduino.os, "execv", side_effect=lambda *args: events.append("exec")) as execute,
        ):
            self.assertEqual(stream_arduino.main(["old.mp4", "--no-audio-reactive", "--no-browser",
                                                  "--fps", "24", "--controls-port", "0"]), 0)
            carried_state = take_reload_state()
        self.assertEqual(events, ["http closed", "serial closed", "exec"])
        options = stream_arduino.build_parser().parse_args(execute.call_args.args[1][2:])
        self.assertEqual(options.video, Path("/tmp/youtube.mp4"))
        self.assertEqual(options.fps, 24)
        self.assertEqual(options.controls_port, 49123)
        self.assertEqual(options.start, 0)
        self.assertTrue(options.no_audio_reactive)
        self.assertTrue(options.loop)
        self.assertEqual(carried_state["video"], {
            "paused": False, "layers": {"video": True, "audio_palette": False},
        })


if __name__ == "__main__":
    unittest.main()
