import io
from pathlib import Path
import struct
import unittest
from unittest import mock

import numpy as np

from led_animator import VideoInfo
from mp4_player import player
from mp4_player import stream


class BufferedMp4PlayerTests(unittest.TestCase):
    def test_preparation_uses_existing_blend_algorithm_for_48x48(self):
        # Every output pixel is the rounded average of one 2x2 source block.
        block = np.array(
            [[[0, 4, 8], [4, 8, 12]], [[8, 12, 16], [12, 16, 20]]],
            dtype=np.uint8,
        )
        frame = np.tile(block, (48, 48, 1))
        process = mock.Mock(stdout=io.BytesIO(frame.tobytes()))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(player.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(
                player, "probe_video", return_value=VideoInfo(1920, 1080, 30)
            ),
            mock.patch.object(player.subprocess, "Popen", return_value=process) as spawn,
        ):
            clip = player.prepare_clip(
                Path("clip.mp4"), gamma=1, brightness=1, smooth_ms=0
            )

        self.assertEqual(clip.fps, 30)
        self.assertEqual(clip.frames, 1)
        self.assertEqual(len(clip.data), 48 * 48 * 2)
        # [6, 10, 14] encoded as RGB565.
        expected = ((6 >> 3) << 11) | ((10 >> 2) << 5) | (14 >> 3)
        self.assertEqual(struct.unpack_from("<H", clip.data)[0], expected)
        command = " ".join(spawn.call_args.args[0])
        self.assertIn("scale=96:96:flags=area", command)
        self.assertIn("fps=30", command)

    def test_native_48_video_is_not_upscaled_for_blending(self):
        self.assertEqual(player._working_size(48, 48), 48)
        self.assertEqual(player._working_size(3840, 2160), 96)

    def test_default_is_board_timed_30_fps(self):
        args = player.build_parser().parse_args(["movie.mp4"])
        self.assertEqual(args.fps, 30)

    def test_rolling_stream_uses_measured_stable_default(self):
        args = stream.build_parser().parse_args(["movie.mp4"])
        self.assertEqual(args.fps, 6)


if __name__ == "__main__":
    unittest.main()
