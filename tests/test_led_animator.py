import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import led_animator
import led_animator_48

from led_animator import (
    LedAnimation,
    VideoInfo,
    build_parser,
    frame_to_led_grid,
    generate_animation,
    load_npz,
    merge_neighboring_pixels,
    render_led_frame,
    save_led_map,
    save_npz,
    save_preview_mp4,
)


def probe_media(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,nb_frames,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class PixelMergingTests(unittest.TestCase):
    def test_merges_each_two_by_two_neighborhood(self):
        frame = np.array(
            [
                [[0, 0, 0], [4, 8, 12], [100, 0, 0], [104, 0, 0]],
                [[8, 12, 16], [12, 16, 20], [108, 0, 0], [112, 0, 0]],
                [[0, 100, 0], [0, 104, 0], [0, 0, 100], [0, 0, 104]],
                [[0, 108, 0], [0, 112, 0], [0, 0, 108], [0, 0, 112]],
            ],
            dtype=np.uint8,
        )
        expected = np.array(
            [[[6, 9, 12], [106, 0, 0]], [[0, 106, 0], [0, 0, 106]]],
            dtype=np.uint8,
        )
        np.testing.assert_array_equal(merge_neighboring_pixels(frame), expected)

    def test_repeated_merging_produces_16_square_rgb_grid(self):
        frame = np.full((64, 64, 3), [12, 34, 56], dtype=np.uint8)
        result = frame_to_led_grid(frame)
        self.assertEqual(result.shape, (16, 16, 3))
        np.testing.assert_array_equal(result, np.broadcast_to([12, 34, 56], result.shape))

    def test_non_power_of_two_input_keeps_full_color(self):
        frame = np.full((45, 45, 3), [250, 20, 99], dtype=np.uint8)
        result = frame_to_led_grid(frame)
        np.testing.assert_array_equal(result, np.broadcast_to([250, 20, 99], result.shape))

    def test_can_generate_a_48_square_rgb_grid(self):
        frame = np.full((96, 96, 3), [12, 34, 56], dtype=np.uint8)
        result = frame_to_led_grid(frame, grid_size=48)
        self.assertEqual(result.shape, (48, 48, 3))
        np.testing.assert_array_equal(result, np.broadcast_to([12, 34, 56], result.shape))


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.animation = LedAnimation(
            np.full((2, 16, 16, 3), [1, 2, 3], dtype=np.uint8), 25.0
        )

    def test_npz_round_trip_is_lossless(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ledanim.npz"
            save_npz(self.animation, path, batch_size=1)
            loaded = load_npz(path)
            self.assertEqual(loaded.fps, 25.0)
            np.testing.assert_array_equal(loaded.frames, self.animation.frames)

    def test_npz_can_be_extracted_to_a_disk_backed_frame_store(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / "sample.ledanim.npz"
            frame_store = folder / "frames.npy"
            save_npz(self.animation, path, batch_size=1)
            loaded = load_npz(path, frame_store)
            try:
                self.assertTrue(frame_store.is_file())
                self.assertEqual(loaded.frames.shape, (2, 16, 16, 3))
                np.testing.assert_array_equal(loaded.frames, self.animation.frames)
            finally:
                loaded.close()

    def test_json_has_board_metadata_and_led_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ledmap.json"
            save_led_map(self.animation, path)
            data = json.loads(path.read_text())
            self.assertEqual(data["width"], 16)
            self.assertEqual(data["height"], 16)
            self.assertEqual(data["frame_duration_us"], 40_000)
            self.assertEqual(data["frames"][0][0][0], [1, 2, 3])

    def test_preview_dimensions_include_leds_and_gaps(self):
        image = render_led_frame(self.animation.frames[0], led_size=10, gap=2)
        self.assertEqual(image.size, (194, 194))

    def test_preview_adds_brightness_and_a_colored_halo(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        frame[8, 8] = [96, 16, 4]
        image = render_led_frame(frame, led_size=10, gap=6)

        pitch = 16
        led_left = 6 + 8 * pitch
        led_top = 6 + 8 * pitch
        center = image.getpixel((led_left + 4, led_top + 4))
        halo = image.getpixel((led_left - 2, led_top + 4))

        self.assertGreater(center[0], 96)
        self.assertGreater(center[0], center[1])
        self.assertGreater(halo[0], 0)
        self.assertGreater(halo[0], halo[1])

    def test_unlit_led_does_not_create_a_halo(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        image = render_led_frame(frame, led_size=10, gap=6)
        self.assertEqual(image.getpixel((3, 11)), (0, 0, 0))

    def test_default_mp4_preview_is_256_pixels_at_up_to_60_fps(self):
        args = build_parser().parse_args(["input.mp4"])
        self.assertEqual(args.led_size, 16)
        self.assertEqual(args.gap, 0)
        self.assertEqual(args.preview_fps, 60.0)
        self.assertEqual(args.batch_size, 16)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.mp4"
            save_preview_mp4(self.animation, path)
            stream = probe_media(path)["streams"][0]
            self.assertEqual((stream["width"], stream["height"]), (256, 256))

    def test_mp4_reduces_only_preview_fps_and_preserves_duration(self):
        frames = np.empty((60, 16, 16, 3), dtype=np.uint8)
        for index, frame in enumerate(frames):
            frame[:] = [index * 4, index * 3, index * 2]
        animation = LedAnimation(frames, 60.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.mp4"
            save_preview_mp4(
                animation,
                path,
                led_size=2,
                gap=0,
                preview_fps=12.0,
            )
            media = probe_media(path)
            self.assertEqual(int(media["streams"][0]["nb_frames"]), 12)
            self.assertAlmostEqual(float(media["format"]["duration"]), 1.0, places=2)

        self.assertEqual(animation.fps, 60.0)
        self.assertEqual(len(animation.frames), 60)

    def test_mp4_default_preserves_every_source_frame_up_to_60_fps(self):
        frames = np.zeros((60, 16, 16, 3), dtype=np.uint8)
        animation = LedAnimation(frames, 60.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.mp4"
            save_preview_mp4(animation, path, led_size=2, gap=0)
            media = probe_media(path)
            self.assertEqual(int(media["streams"][0]["nb_frames"]), 60)
            self.assertEqual(media["streams"][0]["avg_frame_rate"], "60/1")

    def test_failed_export_does_not_publish_a_partial_output_set(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "movie.mp4"
            video.touch()
            prefix = folder / "result"
            existing_npz = folder / "result.ledanim.npz"
            existing_npz.write_bytes(b"previous complete output")

            def fake_save_npz(animation, path, batch_size):
                path.write_bytes(b"new staged output")

            with (
                mock.patch.object(
                    led_animator,
                    "generate_animation",
                    return_value=self.animation,
                ),
                mock.patch.object(
                    led_animator,
                    "save_npz",
                    side_effect=fake_save_npz,
                ),
                mock.patch.object(
                    led_animator,
                    "save_led_map",
                    side_effect=MemoryError("simulated limit"),
                ),
            ):
                result = led_animator.main(
                    [str(video), "-o", str(prefix), "--no-preview"]
                )

            self.assertEqual(result, 1)
            self.assertEqual(existing_npz.read_bytes(), b"previous complete output")
            self.assertFalse((folder / "result.ledmap.json").exists())
            self.assertFalse(list(folder.glob(".*.work-*")))


class LargeGridEntrypointTests(unittest.TestCase):
    def test_uses_48_grid_and_safe_output_defaults(self):
        original_grid_size = led_animator.GRID_SIZE
        observed = {}

        def fake_main(argv, **kwargs):
            observed["argv"] = argv
            observed["grid_size"] = led_animator.GRID_SIZE
            observed.update(kwargs)
            return 7

        with mock.patch.object(led_animator, "main", side_effect=fake_main):
            result = led_animator_48.main(["movie.mp4"])

        self.assertEqual(result, 7)
        self.assertEqual(observed["argv"], ["movie.mp4"])
        self.assertEqual(observed["grid_size"], 48)
        self.assertEqual(observed["default_led_size"], 8)
        self.assertEqual(observed["default_gap"], 2)
        self.assertEqual(observed["default_output_suffix"], "_48x48")
        self.assertEqual(led_animator.GRID_SIZE, original_grid_size)

    def test_generation_uses_the_active_48_grid(self):
        source_frame = np.full((96, 96, 3), [12, 34, 56], dtype=np.uint8)
        original_grid_size = led_animator.GRID_SIZE
        led_animator.GRID_SIZE = 48
        try:
            with (
                mock.patch.object(
                    led_animator,
                    "probe_video",
                    return_value=VideoInfo(96, 96, 24.0),
                ),
                mock.patch.object(
                    led_animator,
                    "iter_square_video_frames",
                    return_value=iter([source_frame]),
                ),
            ):
                animation = generate_animation(Path("movie.mp4"))
        finally:
            led_animator.GRID_SIZE = original_grid_size

        self.assertEqual(animation.frames.shape, (1, 48, 48, 3))

    def test_disk_backed_generation_writes_frames_without_stacking(self):
        source_frames = [
            np.full((48, 48, 3), value, dtype=np.uint8)
            for value in (10, 20, 30)
        ]
        original_grid_size = led_animator.GRID_SIZE
        led_animator.GRID_SIZE = 48
        try:
            with tempfile.TemporaryDirectory() as directory:
                frame_store = Path(directory) / "frames.rgb"
                with (
                    mock.patch.object(
                        led_animator,
                        "probe_video",
                        return_value=VideoInfo(48, 48, 24.0),
                    ),
                    mock.patch.object(
                        led_animator,
                        "iter_square_video_frames",
                        return_value=iter(source_frames),
                    ),
                    mock.patch.object(
                        np,
                        "stack",
                        side_effect=AssertionError("frames must not be stacked"),
                    ),
                ):
                    animation = generate_animation(Path("movie.mp4"), frame_store)

                try:
                    self.assertEqual(animation.frames.shape, (3, 48, 48, 3))
                    self.assertEqual(
                        frame_store.stat().st_size,
                        3 * 48 * 48 * 3,
                    )
                    self.assertEqual(int(animation.frames[2, 0, 0, 0]), 30)
                finally:
                    animation.close()
        finally:
            led_animator.GRID_SIZE = original_grid_size

    def test_large_grid_preview_renders_led_faces_highlights_and_bloom(self):
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[24, 24] = [128, 16, 4]
        original_grid_size = led_animator.GRID_SIZE
        led_animator.GRID_SIZE = 48
        try:
            image = render_led_frame(frame, led_size=8, gap=2)
        finally:
            led_animator.GRID_SIZE = original_grid_size

        try:
            self.assertEqual(image.size, (482, 482))
            face = image.getpixel((245, 245))
            halo = image.getpixel((241, 245))
            self.assertGreater(face[0], 128)
            self.assertGreater(face[0], face[1])
            self.assertGreater(halo[0], halo[1])
        finally:
            image.close()


if __name__ == "__main__":
    unittest.main()
