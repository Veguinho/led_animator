import io
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zlib
import numpy as np

import main as launcher
import device_modes
from preloaded_video import player


class FakeBoard:
    def __init__(self, capacity=player.MAX_CLIP_BYTES, lose_chunk_ack=False):
        self.capacity = capacity
        self.incoming = bytearray()
        self.data = bytearray()
        self.commands = []
        self.lose_chunk_ack = lose_chunk_ack
        self.timeout = False

    def write(self, packet):
        magic, version, kind, length, sequence, crc = player.HEADER.unpack(packet[:16])
        payload = packet[16:]
        assert magic == b"PV32" and version == 1
        assert len(payload) == length and zlib.crc32(payload) == crc
        self.commands.append(kind)
        value = 0
        if kind == player.INFO:
            value = self.capacity
        elif kind == player.BEGIN:
            self.frames, self.fps, self.checksum = struct.unpack("<III", payload)
        elif kind == player.CHUNK:
            offset = struct.unpack_from("<I", payload)[0]
            if offset == len(self.data):
                self.data.extend(payload[4:])
            else:
                assert self.data[offset:offset+length-4] == payload[4:]
            value = len(self.data)
            if self.lose_chunk_ack:
                self.lose_chunk_ack = False
                self.timeout = True
                return
        elif kind in (player.PLAY, player.VERIFY):
            assert len(self.data) == self.frames * player.FRAME_BYTES
            assert zlib.crc32(self.data) == self.checksum
        self.incoming.extend(b"boot log\n" + player.RESPONSE.pack(b"PVOK", 1, 1, 0, sequence, value, 0, 0))

    def read(self, size=1):
        if self.timeout:
            self.timeout = False
            raise TimeoutError("simulated lost ACK")
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def flush(self):
        pass


class PreloadedVideoTests(unittest.TestCase):
    def test_video_preparation_preserves_native_48_pixel_corners(self):
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[0, 0] = (255, 0, 0)
        frame[47, 47] = (0, 0, 255)
        process = mock.Mock(stdout=io.BytesIO(frame.tobytes()))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(player.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(player.subprocess, "Popen", return_value=process) as spawn,
        ):
            clip = player.prepare_clip(Path("clip.mp4"), 30, gamma=1, brightness=1, smooth_ms=0)
        self.assertEqual(len(clip.data), 4608)
        self.assertEqual(clip.frames, 1)
        self.assertEqual(struct.unpack_from("<H", clip.data, 0)[0], 0xf800)
        self.assertEqual(struct.unpack_from("<H", clip.data, 4606)[0], 0x001f)
        self.assertIn("scale=48:48:flags=area", " ".join(spawn.call_args.args[0]))

    def test_video_connect_keeps_boot_speed_separate_from_live_default(self):
        connection = mock.Mock(baudrate=player.BOOT_BAUD)
        with (
            mock.patch.object(player, "open_serial", return_value=connection) as opened,
            mock.patch.object(player, "exchange"),
            mock.patch.object(player.time, "sleep"),
        ):
            self.assertIs(player.connect_video("test-port", 2000000), connection)
        opened.assert_called_once_with("test-port", 2, baudrate=230400)
        self.assertEqual(connection.baudrate, 2000000)

    def test_benchmark_verifies_data_without_playing(self):
        board = FakeBoard()
        clip = player.Clip(bytes(range(256)) * 36, 30)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            player.preload(board, clip, play=False)
        self.assertEqual(board.commands[-1], player.VERIFY)
        self.assertNotIn(player.PLAY, board.commands)

    def test_baud_change_is_acknowledged_then_confirmed(self):
        connection = mock.Mock(baudrate=player.BOOT_BAUD)
        speeds = []
        def exchange(*args, **kwargs):
            speeds.append((args[1], connection.baudrate))
        with mock.patch.object(player, "exchange", side_effect=exchange), mock.patch.object(player.time, "sleep"):
            player.set_baud(connection, 2000000)
        self.assertEqual(speeds, [(player.BAUD, player.BOOT_BAUD), (player.INFO, 2000000)])

    def test_failed_baud_confirmation_restores_host_boot_speed(self):
        connection = mock.Mock(baudrate=player.BOOT_BAUD)
        with mock.patch.object(player, "exchange", side_effect=[None, RuntimeError("timeout")]), mock.patch.object(player.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "could not confirm"):
                player.set_baud(connection, 2000000)
        self.assertEqual(connection.baudrate, player.BOOT_BAUD)
        connection.reset_input_buffer.assert_called_once()

    def test_softening_attenuates_flashes_and_smooths_loop_seam(self):
        frames = np.zeros((60, 1, 1, 3), dtype=np.uint8)
        frames[-1] = 255
        softened = player.soften_frames(frames, 30, 0.25, 180)
        self.assertLessEqual(int(softened.max()), 12)
        # The final flash decays through the beginning of the next loop.
        self.assertGreater(int(softened[0, 0, 0, 0]), 0)
        jumps = np.abs(np.diff(np.concatenate([softened[-1:], softened]).astype(int), axis=0))
        self.assertLessEqual(int(jumps.max()), 12)
        steady = player.soften_frames(np.full_like(frames, 200), 30, 0.25, 180)
        np.testing.assert_array_equal(steady, 50)

    def test_chunk_retry_preserves_clip_and_play_waits_for_complete_upload(self):
        board = FakeBoard(lose_chunk_ack=True)
        clip = player.Clip(bytes(range(256)) * 36, 60)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            player.preload(board, clip)
        self.assertEqual(bytes(board.data), clip.data)
        self.assertEqual(board.commands[-1], player.PLAY)
        self.assertEqual(board.commands.count(player.CHUNK), 4)

    def test_capacity_failure_does_not_begin_upload(self):
        board = FakeBoard(capacity=100)
        with self.assertRaisesRegex(ValueError, "board offers 100"):
            player.preload(board, player.Clip(bytes(player.FRAME_BYTES), 60))
        self.assertEqual(board.commands, [player.INFO])

    def test_prepare_only_never_changes_board_or_stops_audio(self):
        with (
            mock.patch.object(player, "prepare_clip", return_value=player.Clip(bytes(player.FRAME_BYTES), 60)),
            mock.patch.object(player, "flash_firmware") as flash,
            mock.patch.object(player, "stop_mode_workers") as stop,
            mock.patch.object(player, "resolve_port") as port,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(player.main(["clip.mp4", "--prepare-only"]), 0)
        flash.assert_not_called()
        stop.assert_not_called()
        port.assert_not_called()

    def test_menu_dispatches_video_path_with_spaces(self):
        with (
            mock.patch("builtins.input", side_effect=["2", "my clip.mp4"]),
            mock.patch.object(launcher.subprocess, "call", return_value=0) as call,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(launcher.main([]), 0)
        self.assertEqual(call.call_args.args[0][-1], "my clip.mp4")

    def test_audio_mode_flashes_audio_and_forwards_options(self):
        with (
            mock.patch.object(launcher, "resolve_port", return_value="test-port"),
            mock.patch.object(launcher, "flash_firmware") as flash,
            mock.patch.object(launcher.subprocess, "call", return_value=0) as call,
        ):
            self.assertEqual(launcher.main(["audio", "--slowdown", "50"]), 0)
        flash.assert_called_once_with("audio", "test-port")
        self.assertEqual(call.call_args.args[0][-2:], ["--slowdown", "50"])

    def test_mode_cleanup_does_not_touch_other_checkouts_or_itself(self):
        root = device_modes.ROOT
        processes = (f"100 /Library/Frameworks/Python.framework/Versions/3.13/Python {root}/system_audio_visualizer.py\n"
                     f"101 python /other/system_audio_visualizer.py\n"
                     f"102 python {root}/preloaded_video/player.py clip.mp4\n")
        with (
            mock.patch.object(device_modes.subprocess, "check_output", return_value=processes),
            mock.patch("audio_service.stop") as stop_service,
            mock.patch.object(device_modes.os, "getpid", return_value=102),
            mock.patch.object(device_modes.os, "kill", side_effect=[None, ProcessLookupError]) as kill,
            mock.patch.object(device_modes.time, "sleep"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            device_modes.stop_mode_workers()
            stop_service.assert_called_once_with()
        self.assertEqual([call.args[0] for call in kill.call_args_list], [100, 100])

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler required")
    def test_firmware_capacity_crc_retry_and_playback_timing(self):
        source = r'''
#include <cassert>
#include <vector>
#include "clip_buffer.h"
int main() {
  static_assert(FRAME_BYTES == 4608);
  std::vector<uint8_t> storage(FRAME_BYTES * 3), frames(FRAME_BYTES * 3, 42);
  ClipBuffer clip;
  clip.data = storage.data(); clip.capacity = storage.size();
  assert(!clip.begin(4,60,0)); assert(!clip.begin(0,60,0));
  assert(!clip.begin(3,61,0)); assert(!clip.begin(3,0,0));
  assert((updateCrc(0xffffffffUL,(const uint8_t*)"123456789",9)^0xffffffffUL) == 0xcbf43926);
  auto crc = updateCrc(0xffffffffUL,frames.data(),frames.size())^0xffffffffUL;
  assert(clip.begin(3,60,crc));
  assert(!clip.append(1,frames.data(),10));
  assert(clip.append(0,frames.data(),4096));
  auto partialCrc = clip.crc;
  assert(clip.append(0,frames.data(),4096));
  assert(clip.received == 4096 && clip.crc == partialCrc);
  assert(!clip.complete());
  assert(!clip.append(0xffffffffUL,frames.data(),1));
  assert(clip.append(4096,frames.data()+4096,frames.size()-4096));
  assert(clip.complete());
  assert(clip.begin(3,60,crc+1));
  assert(clip.append(0,frames.data(),frames.size()));
  assert(!clip.complete());
  PlaybackClock clock;
  clock.play(1000000,true);
  assert(clock.next(1000000,60,3) == 0);
  assert(clock.next(1016666,60,3) == -1);
  assert(clock.next(1016667,60,3) == 1);
  assert(clock.next(1050000,60,3) == 0);
  assert(clock.missed == 1 && clock.displayed == 3);
  clock.play(0,false);
  for (uint32_t i=0; i<60; ++i)
    assert(clock.next((i*1000000ULL+59)/60,60,60) == int(i));
  assert(clock.next(1000000,60,60) == -1 && !clock.playing);
  assert(clock.displayed == 60 && clock.missed == 0);
}
'''
        folder = device_modes.ROOT / "preloaded_video/preloaded_video_firmware"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "test.cpp").write_text(source)
            subprocess.run(["c++", "-std=c++17", "-I", str(folder), str(path / "test.cpp"),
                            "-o", str(path / "test")], check=True, capture_output=True)
            subprocess.run([str(path / "test")], check=True, capture_output=True)
