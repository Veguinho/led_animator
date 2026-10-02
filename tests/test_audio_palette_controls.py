import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from audio_palette_controls import (
    PaletteControls, PaletteServer, default_settings, make_palette,
)
from video_controls import VideoPlaybackControls


class PaletteTests(unittest.TestCase):
    def test_palette_survives_restart_and_invalid_updates_do_not_overwrite_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "palette.json"
            controls = PaletteControls(size=32, settings_path=path)
            controls.update(default_settings() | {"preset": "sunset", "brightness": .296})
            saved = controls.state()["settings"]
            with self.assertRaises(ValueError):
                controls.update(saved | {"brightness": 2})
            restored = PaletteControls(size=32, settings_path=path)
            self.assertEqual(restored.state()["settings"], saved)
            np.testing.assert_array_equal(restored.palette_snapshot()[1], controls.palette_snapshot()[1])

    def test_invalid_saved_palette_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "palette.json"
            for content in ('{', '{"brightness": 1}'):
                path.write_text(content)
                self.assertEqual(PaletteControls(settings_path=path).state()["settings"], default_settings())

    def test_combination_modes_and_reverse(self):
        settings = default_settings()
        settings.update(preset="custom", colors=["#ff0000", "#0000ff"], brightness=1)
        gradient = make_palette(settings)
        self.assertTrue(np.all(gradient[1:-1, 0] > 0))
        self.assertTrue(np.all(gradient[1:-1, 2] > 0))
        settings["reverse"] = True
        np.testing.assert_array_equal(make_palette(settings), gradient[::-1])
        settings.update(reverse=False, blend="bands")
        bands = make_palette(settings)
        np.testing.assert_array_equal(bands[:8], np.tile([255, 0, 0], (8, 1)))
        np.testing.assert_array_equal(bands[8:], np.tile([0, 0, 255], (8, 1)))

    def test_single_color_brightness_and_desaturation(self):
        settings = default_settings()
        settings.update(preset="custom", colors=["#ff0000"], brightness=0.5)
        np.testing.assert_array_equal(make_palette(settings), np.tile([128, 0, 0], (16, 1)))
        settings["saturation"] = 0
        np.testing.assert_array_equal(make_palette(settings), np.full((16, 3), 128))

    def test_invalid_updates_preserve_previous_state(self):
        controls = PaletteControls()
        original = controls.state()
        for changes in (
            {"colors": []}, {"colors": ["red"]}, {"colors": ["#ff0000"] * 7},
            {"brightness": float("nan")}, {"brightness": -1},
            {"brightness": True}, {"saturation": 1.1}, {"reverse": "yes"},
            {"preset": []}, {"blend": {}}, {"extra": 1},
            {"slowdown": -1}, {"slowdown": 100}, {"slowdown": True},
            {"slowdown": float("nan")}, {"slowdown": float("inf")},
            {"slowdown": "50"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    controls.update(default_settings() | changes)
                self.assertEqual(controls.state(), original)

    def test_snapshots_do_not_expose_mutable_state(self):
        controls = PaletteControls()
        snapshot = controls.state()
        snapshot["settings"]["colors"][0] = "#000000"
        _, palette, _ = controls.palette_snapshot()
        palette.fill(0)
        self.assertEqual(controls.state()["settings"], default_settings())
        self.assertTrue(np.any(controls.palette_snapshot()[1]))


class PaletteServerTests(unittest.TestCase):
    def setUp(self):
        self.controls = PaletteControls()
        self.server = PaletteServer(self.controls, port=0)
        self.server.start()
        self.addCleanup(self.server.close)

    def post(self, payload, **headers):
        request = Request(
            self.server.url + "/api/palette", data=payload,
            headers={"Content-Type": "application/json", **headers}, method="POST",
        )
        return urlopen(request, timeout=2)

    def test_page_and_live_update_round_trip(self):
        with urlopen(self.server.url, timeout=2) as response:
            self.assertIn(b"Live palette", response.read())
        settings = default_settings() | {"preset": "ocean", "brightness": 0.4, "slowdown": 75}
        with self.post(json.dumps(settings).encode(), Origin=self.server.url) as response:
            saved = json.load(response)
        self.assertEqual(saved["settings"]["preset"], "ocean")
        self.assertEqual(saved["settings"]["brightness"], 0.4)
        self.assertEqual(saved["settings"]["slowdown"], 75)
        with urlopen(self.server.url + "/api/state", timeout=2) as response:
            self.assertEqual(json.load(response), saved)

    def test_malformed_and_cross_origin_updates_are_rejected(self):
        for payload, headers, expected in (
            (b"{", {}, 400), (b"null", {}, 400),
            (json.dumps(default_settings()).encode(), {"Origin": "https://example.com"}, 403),
        ):
            with self.subTest(payload=payload, headers=headers):
                with self.assertRaises(HTTPError) as error:
                    self.post(payload, **headers)
                self.assertEqual(error.exception.code, expected)
                error.exception.close()
                self.assertEqual(self.controls.state()["revision"], 0)

    def test_video_tab_seeks_only_with_valid_local_time(self):
        video = VideoPlaybackControls("Wex.mp4", 120.0, 24.0)
        server = PaletteServer(PaletteControls(), port=0, video_controls=video)
        server.start()
        self.addCleanup(server.close)
        with urlopen(server.url + "/api/state", timeout=2) as response:
            state = json.load(response)
        self.assertEqual(state["mode"], "video")
        self.assertEqual(state["video"]["duration"], 120.0)
        with urlopen(server.url, timeout=2) as response:
            self.assertIn(b'id="video-tab"', response.read())
        seek = Request(
            server.url + "/api/video/seek", data=b'{"seconds": 65}',
            headers={"Content-Type": "application/json", "Origin": server.url},
            method="POST",
        )
        with urlopen(seek, timeout=2) as response:
            self.assertEqual(json.load(response)["video"]["position"], 65.0)
        self.assertEqual(video.take_seek(), 65.0)
        for payload in (b'{"seconds": -1}', b'{"seconds": 120}', b'{"seconds": true}'):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                urlopen(Request(server.url + "/api/video/seek", data=payload,
                                headers={"Content-Type": "application/json", "Origin": server.url},
                                method="POST"), timeout=2)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

    def test_video_pause_resume_and_seek_preserve_playback_state(self):
        video = VideoPlaybackControls("clip.mp4", 120, 24, start=15)
        server = PaletteServer(PaletteControls(), port=0, video_controls=video)
        server.start()
        self.addCleanup(server.close)

        def playback(payload, **headers):
            return urlopen(Request(server.url + "/api/video/playback",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", **headers}), timeout=2)

        with playback({"paused": True}, Origin=server.url) as response:
            paused = json.load(response)["video"]
        self.assertTrue(paused["paused"])
        self.assertFalse(paused["playing"])
        self.assertEqual(paused["position"], 15)
        self.assertTrue(video.seek(30)["paused"])
        self.assertFalse(video.state()["playing"])
        self.assertEqual(video.take_seek(), 30)
        with playback({"paused": False}, Origin=server.url) as response:
            resumed = json.load(response)["video"]
        self.assertFalse(resumed["paused"])
        self.assertTrue(resumed["playing"])
        self.assertEqual(resumed["position"], 30)
        for payload, headers, expected in (
            ({"paused": 1}, {}, 400), ({"paused": "true"}, {}, 400),
            ({"paused": None}, {}, 400), ({}, {}, 400), (None, {}, 400),
            ({"paused": True, "extra": 1}, {}, 400),
            ({"paused": True}, {"Origin": "https://example.com"}, 403),
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                playback(payload, **headers)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
            self.assertTrue(video.state()["playing"])

    def test_pause_without_running_video_is_unavailable(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.server.url + "/api/video/playback", data=b'{"paused":true}',
                headers={"Content-Type": "application/json"}, method="POST"), timeout=2)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_video_texture_mapping_toggle_and_invalid_requests(self):
        video = VideoPlaybackControls("clip.mp4", 60, 30, start=15)
        video.set_paused(True)
        server = PaletteServer(PaletteControls(), port=0, video_controls=video)
        server.start()
        self.addCleanup(server.close)

        def mapping(payload, **headers):
            return urlopen(Request(server.url + "/api/video/texture-mapping",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", **headers}), timeout=2)

        for enabled in (False, True):
            with mapping({"enabled": enabled}, Origin=server.url) as response:
                state = json.load(response)["video"]
            self.assertEqual(state["texture_mapping_enabled"], enabled)
            self.assertEqual(state["position"], 15)
            self.assertTrue(state["paused"])
            with urlopen(server.url + "/api/state", timeout=2) as response:
                self.assertEqual(json.load(response)["video"], state)
        for payload, headers, expected in (
            ({"enabled": 1}, {}, 400), ({"enabled": "false"}, {}, 400),
            ({"enabled": None}, {}, 400), ({}, {}, 400), (None, {}, 400),
            ({"enabled": False, "extra": 1}, {}, 400),
            ({"enabled": False}, {"Origin": "https://example.com"}, 403),
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                mapping(payload, **headers)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
            self.assertTrue(video.texture_mapping_enabled())
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.server.url + "/api/video/texture-mapping",
                data=b'{"enabled":false}', method="POST",
                headers={"Content-Type": "application/json"}), timeout=2)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_output_layers_toggle_independently_and_validate_atomically(self):
        video = VideoPlaybackControls("clip.mp4", 60, 30, start=15)
        video.set_paused(True)
        server = PaletteServer(PaletteControls(), port=0, video_controls=video)
        server.start()
        self.addCleanup(server.close)

        def layers(payload, **headers):
            return urlopen(Request(server.url + "/api/video/layers",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Content-Type": "application/json", **headers}), timeout=2)

        for payload, expected in (
            ({"video": False}, {"video": False, "audio_palette": True}),
            ({"audio_palette": False}, {"video": False, "audio_palette": False}),
            ({"video": True}, {"video": True, "audio_palette": False}),
            ({"audio_palette": True}, {"video": True, "audio_palette": True}),
        ):
            with layers(payload, Origin=server.url) as response:
                state = json.load(response)["video"]
            self.assertEqual(state["layers"], expected)
            self.assertEqual(state["position"], 15)
            self.assertTrue(state["paused"])
            with urlopen(server.url + "/api/state", timeout=2) as response:
                self.assertEqual(json.load(response)["video"], state)
        original = video.state()
        for payload, headers, code in (
            ({}, {}, 400), (None, {}, 400), ([], {}, 400),
            ({"video": 0}, {}, 400), ({"audio_palette": "false"}, {}, 400),
            ({"video": False, "audio_palette": None}, {}, 400),
            ({"video": False, "unknown": True}, {}, 400),
            ({"video": False}, {"Origin": "https://example.com"}, 403),
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPError) as error:
                layers(payload, **headers)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
            self.assertEqual(video.state(), original)
        snapshot = video.output_layers()
        snapshot["video"] = False
        self.assertTrue(video.output_layers()["video"])
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.server.url + "/api/video/layers",
                data=b'{"video":false}', method="POST",
                headers={"Content-Type": "application/json"}), timeout=2)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()

    def test_offline_video_preview_serves_byte_ranges_and_accepts_seek(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "preview.mp4"
            clip.write_bytes(b"0123456789abcdef")
            video = VideoPlaybackControls("preview.mp4", 12.0, 24.0,
                                          connected=False)
            server = PaletteServer(PaletteControls(), port=0,
                                   video_controls=video, video_path=clip)
            server.start()
            try:
                with urlopen(Request(server.url + "/api/video/file",
                                     headers={"Range": "bytes=5-9"}), timeout=2) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Range"], "bytes 5-9/16")
                    self.assertEqual(response.read(), b"56789")
                with urlopen(Request(server.url + "/api/video/file", method="HEAD"), timeout=2) as response:
                    self.assertEqual(response.headers["Content-Length"], "16")
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(server.url + "/api/video/file",
                                    headers={"Range": "bytes=99-"}), timeout=2)
                self.assertEqual(error.exception.code, 416)
                error.exception.close()
                video.seek(3)
                self.assertIsNone(video.take_seek())
                self.assertFalse(video.state()["connected"])
                self.assertEqual(video.state()["position"], 3)
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
