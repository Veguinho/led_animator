import struct
import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np

import stream_arduino
from led_animator import VideoInfo
from stream_arduino import (
    PACKET_FRAME,
    PACKET_HEADER,
    PROTOCOL_VERSION,
    REQUEST_MAGIC,
    RESPONSE,
    RESPONSE_MAGIC,
    STATUS_ACK,
    build_packet,
    open_video,
    read_response,
    resolve_port,
)


class FakeSerial:
    def __init__(self, incoming: bytes):
        self.incoming = bytearray(incoming)

    def read(self, size: int = 1) -> bytes:
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result


class StreamingProtocolTests(unittest.TestCase):
    def test_live_stream_rebases_after_cpu_stall_without_catchup_burst(self):
        source = stream_arduino.FrameSource(
            10,
            lambda: iter([bytes([value]) * 512 for value in range(3)]),
        )
        clock = mock.Mock(side_effect=[
            0.0,  # statistics start
            0.0, 0.0,  # first frame deadline and current time
            0.0,  # first frame statistics
            0.35,  # second frame current time: CPU stalled
            0.35,  # second frame statistics
            0.35,  # third frame current time
            0.45,  # third frame statistics after its paced sleep
        ])
        sent_at = []

        def exchange(*_args):
            sent_at.append(clock.call_args_list[-1])

        with (
            mock.patch.object(stream_arduino.time, "monotonic", clock),
            mock.patch.object(stream_arduino.time, "sleep") as sleep,
            mock.patch.object(stream_arduino, "exchange_packet", side_effect=exchange),
        ):
            result = stream_arduino.stream_frames(
                mock.Mock(), source, loop=False, timeout=1, retries=0,
                drop_late=False, rebase_late=True, display_size=16,
            )

        self.assertEqual(result, (3, 0))
        self.assertEqual(len(sent_at), 3)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.1)

    def test_live_stream_rejects_conflicting_late_policies(self):
        source = stream_arduino.FrameSource(10, lambda: iter([bytes(512)]))
        with self.assertRaisesRegex(ValueError, "cannot both"):
            stream_arduino.stream_frames(
                mock.Mock(), source, loop=False, timeout=1, retries=0,
                drop_late=True, rebase_late=True,
            )

    def test_48_pixel_handshake(self):
        connection = mock.Mock()
        with mock.patch.object(stream_arduino, "exchange_packet") as exchange:
            stream_arduino.handshake(connection, 60, 1, 3, display_size=48)
        self.assertEqual(
            stream_arduino.HELLO.unpack(exchange.call_args.args[3]),
            (48, 48, 1, 0, 16667),
        )

    def test_existing_effect_pixels_expand_to_three_by_three_blocks(self):
        pixels = np.arange(256, dtype="<u2").reshape(16, 16)
        payload = stream_arduino.resize_rgb565(pixels.tobytes(), 16, 48)
        result = np.frombuffer(payload, dtype="<u2").reshape(48, 48)
        self.assertEqual(len(payload), 4608)
        for row in range(16):
            for column in range(16):
                self.assertTrue(np.all(result[row*3:row*3+3, column*3:column*3+3] == pixels[row, column]))

    def test_native_48_pixel_frame_keeps_every_pixel(self):
        payload = np.arange(48*48, dtype="<u2").tobytes()
        self.assertEqual(stream_arduino.resize_rgb565(payload, 48, 48), payload)
        with self.assertRaisesRegex(ValueError, "expected 4608"):
            stream_arduino.resize_rgb565(payload[:-2], 48, 48)

    def test_video_decodes_at_native_screen_resolution(self):
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[47, 47] = (255, 0, 0)
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(stream_arduino, "probe_video", return_value=VideoInfo(1920, 1080, 60)),
            mock.patch.object(stream_arduino, "iter_square_video_frames", return_value=iter([frame])) as decode,
        ):
            source = open_video(Path("sample.mp4"), target_fps=60, size=48)
            payload = next(source.iter_frames())
        self.assertEqual(source.size, 48)
        self.assertEqual(decode.call_args.args[2], 48)
        self.assertEqual(len(payload), 4608)
        self.assertEqual(struct.unpack_from("<H", payload, 4606)[0], 0xf800)

    def test_stream_transmits_full_screen_payload(self):
        source = stream_arduino.FrameSource(60, lambda: iter([bytes(512)]))
        with mock.patch.object(stream_arduino, "exchange_packet") as exchange:
            result = stream_arduino.stream_frames(
                mock.Mock(), source, loop=False, timeout=1, retries=0,
                drop_late=False, display_size=48,
            )
        self.assertEqual(result, (1, 0))
        self.assertEqual(len(exchange.call_args.args[3]), 4608)

    def test_bounded_buffer_preserves_frame_order_without_loading_whole_video(self):
        produced = []

        def frames():
            for index in range(20):
                produced.append(index)
                yield bytes([index])

        source = stream_arduino.FrameSource(10, frames, size=48)
        buffered = stream_arduino.buffered_source(
            source, buffer_seconds=0.5, prebuffer_seconds=0.3
        )
        iterator = buffered.iter_frames()
        first = next(iterator)
        self.assertEqual(first, bytes([0]))
        # The producer can be one frame past the five-slot queue because it may
        # already hold the next yielded object, but it cannot decode the file.
        self.assertLessEqual(len(produced), 7)
        self.assertEqual([first, *iterator], [bytes([i]) for i in range(20)])

    def test_bounded_buffer_validates_window_sizes(self):
        source = stream_arduino.FrameSource(30, lambda: iter(()), size=48)
        with self.assertRaisesRegex(ValueError, "positive"):
            stream_arduino.buffered_source(source, 0, 0)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            stream_arduino.buffered_source(source, 1, 2)

    def test_32_pixel_handshake(self):
        connection = mock.Mock()
        with mock.patch.object(stream_arduino, "exchange_packet") as exchange:
            stream_arduino.handshake(connection, 60, 1, 3, display_size=32)
        self.assertEqual(
            stream_arduino.HELLO.unpack(exchange.call_args.args[3]),
            (32, 32, 1, 0, 16667),
        )

    def test_existing_effect_pixels_expand_to_two_by_two_blocks(self):
        pixels = np.arange(256, dtype="<u2").reshape(16, 16)
        payload = stream_arduino.resize_rgb565(pixels.tobytes(), 16, 32)
        result = np.frombuffer(payload, dtype="<u2").reshape(32, 32)
        self.assertEqual(len(payload), 2048)
        for row in range(16):
            for column in range(16):
                self.assertTrue(np.all(result[row*2:row*2+2, column*2:column*2+2] == pixels[row, column]))

    def test_native_32_pixel_frame_keeps_every_pixel(self):
        payload = np.arange(32*32, dtype="<u2").tobytes()
        self.assertEqual(stream_arduino.resize_rgb565(payload, 32, 32), payload)
        with self.assertRaisesRegex(ValueError, "expected 2048"):
            stream_arduino.resize_rgb565(payload[:-2], 32, 32)

    def test_video_decodes_at_native_32_screen_resolution(self):
        frame = np.zeros((32, 32, 3), dtype=np.uint8)
        frame[31, 31] = (255, 0, 0)
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(stream_arduino, "probe_video", return_value=VideoInfo(1920, 1080, 60)),
            mock.patch.object(stream_arduino, "iter_square_video_frames", return_value=iter([frame])) as decode,
        ):
            source = open_video(Path("sample.mp4"), target_fps=60, size=32)
            payload = next(source.iter_frames())
        self.assertEqual(source.size, 32)
        self.assertEqual(decode.call_args.args[2], 32)
        self.assertEqual(len(payload), 2048)
        self.assertEqual(struct.unpack_from("<H", payload, 2046)[0], 0xf800)

    def test_stream_transmits_32_screen_payload(self):
        source = stream_arduino.FrameSource(60, lambda: iter([bytes(512)]))
        with mock.patch.object(stream_arduino, "exchange_packet") as exchange:
            result = stream_arduino.stream_frames(
                mock.Mock(), source, loop=False, timeout=1, retries=0,
                drop_late=False, display_size=32,
            )
        self.assertEqual(result, (1, 0))
        self.assertEqual(len(exchange.call_args.args[3]), 2048)

    def test_frame_packet_contains_length_sequence_and_crc(self):
        payload = bytes(range(256)) * 2
        packet = build_packet(PACKET_FRAME, 42, payload)
        magic, version, packet_type, length, sequence, crc = PACKET_HEADER.unpack(
            packet[: PACKET_HEADER.size]
        )

        self.assertEqual(magic, REQUEST_MAGIC)
        self.assertEqual(version, PROTOCOL_VERSION)
        self.assertEqual(packet_type, PACKET_FRAME)
        self.assertEqual(length, 512)
        self.assertEqual(sequence, 42)
        self.assertEqual(crc, zlib.crc32(payload))
        self.assertEqual(packet[PACKET_HEADER.size :], payload)

    def test_response_reader_ignores_boot_text(self):
        response = RESPONSE.pack(RESPONSE_MAGIC, PROTOCOL_VERSION, STATUS_ACK, 0, 19)
        result = read_response(FakeSerial(b"ESP-ROM boot message\n" + response), 0.1)

        self.assertEqual(result.status, STATUS_ACK)
        self.assertEqual(result.detail, 0)
        self.assertEqual(result.sequence, 19)

    def test_video_is_sampled_and_compiled_to_rgb565(self):
        frames = []
        for index in range(6):
            frame = np.zeros((16, 16, 3), dtype=np.uint8)
            frame[0, 0] = [index * 40, 0, 0]
            frames.append(frame)

        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                stream_arduino,
                "probe_video",
                return_value=VideoInfo(16, 16, 6.0),
            ),
            mock.patch.object(
                stream_arduino,
                "iter_square_video_frames",
                return_value=iter(frames),
            ),
        ):
            source = open_video(Path("sample.mp4"), target_fps=2.0)
            compiled = list(source.iter_frames())

        self.assertEqual(source.fps, 2.0)
        self.assertEqual(len(compiled), 2)
        self.assertTrue(all(len(frame) == 512 for frame in compiled))
        first_red = struct.unpack_from("<H", compiled[0])[0]
        second_red = struct.unpack_from("<H", compiled[1])[0]
        self.assertEqual(first_red, 0)
        # Gamma 2.2 maps video red 120 to LED intensity 49 before RGB565.
        self.assertEqual(second_red, (49 >> 3) << 11)

    def test_rejects_invalid_target_fps(self):
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                stream_arduino,
                "probe_video",
                return_value=VideoInfo(16, 16, 30.0),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "positive finite"):
                open_video(Path("sample.mp4"), target_fps=float("nan"))

    def test_auto_port_prefers_the_only_usb_controller(self):
        ports = [
            "/dev/cu.Bluetooth-Incoming-Port",
            "/dev/cu.Headphones",
            "/dev/cu.usbserial-1410",
        ]
        with mock.patch.object(stream_arduino, "list_serial_ports", return_value=ports):
            self.assertEqual(resolve_port("auto"), "/dev/cu.usbserial-1410")

    def test_auto_port_waits_for_board_after_cable_is_connected(self):
        ports_before = [
            "/dev/cu.Bluetooth-Incoming-Port",
            "/dev/cu.Headphones",
        ]
        ports_after = ports_before + ["/dev/cu.usbserial-1410"]
        with (
            mock.patch.object(
                stream_arduino,
                "list_serial_ports",
                side_effect=[ports_before, ports_after],
            ),
            mock.patch.object(stream_arduino.time, "sleep"),
            mock.patch.object(stream_arduino.sys, "stderr"),
        ):
            self.assertEqual(
                resolve_port("auto", wait_timeout=1.0, poll_interval=0.0),
                "/dev/cu.usbserial-1410",
            )

    def test_auto_port_does_not_select_bluetooth_devices(self):
        ports = [
            "/dev/cu.Bluetooth-Incoming-Port",
            "/dev/cu.JBLFlip5",
        ]
        with mock.patch.object(stream_arduino, "list_serial_ports", return_value=ports):
            with self.assertRaisesRegex(RuntimeError, "no USB serial controller"):
                resolve_port("auto")


if __name__ == "__main__":
    unittest.main()
