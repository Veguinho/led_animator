import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from audio_palette_controls import (
    PaletteControls, PaletteServer, default_settings, make_palette,
)


class PaletteTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
